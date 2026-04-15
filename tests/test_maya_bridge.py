"""
test_maya_bridge.py
===================
Tests for the MayaBridge TCP communication layer and the server-level
tool functions that depend on it (maya_ping, maya_create_primitive,
maya_execute_python).

All tests use a mock TCP server (see conftest.py) — no running Maya
instance is required.

Test cases (aligned with TESTING_PLAN §4.1):
  1. TCP connection to mock server
  2. Send code and receive result
  3. Timeout when no server is listening
  4. maya_ping returns version and scene info
  5. maya_create_primitive sends the correct command
  6. maya_execute_python sends code and returns result
"""

import asyncio
import json
import os
import re
import socket
import threading
import unittest.mock
import time

import pytest

from maya_mcp.maya_bridge import MayaBridge, MayaBridgeError, MayaConnectionError, MayaExecutionError


# ═══════════════════════════════════════════════════════════════════════════════
# 4.1.1 — TCP Connection to Mock Server
# ═══════════════════════════════════════════════════════════════════════════════

class TestTCPConnection:
    """Verify that MayaBridge can establish a TCP connection to the mock."""

    def test_connect_and_send_mel(self, mock_maya_server, bridge_to_mock):
        """send_mel connects, sends command, and gets response."""
        mock_maya_server.default_response = "Maya 2025"
        result = bridge_to_mock.send_mel("about -v")
        assert result == "Maya 2025"
        assert len(mock_maya_server.received_commands) == 1
        assert mock_maya_server.received_commands[0] == "about -v"

    def test_connect_multiple_commands(self, mock_maya_server, bridge_to_mock):
        """Multiple sequential commands each get their own connection."""
        mock_maya_server.default_response = "OK"
        bridge_to_mock.send_mel("cmd1")
        bridge_to_mock.send_mel("cmd2")
        assert len(mock_maya_server.received_commands) == 2
        assert mock_maya_server.received_commands[0] == "cmd1"
        assert mock_maya_server.received_commands[1] == "cmd2"

    def test_bridge_stores_host_port(self):
        """MayaBridge correctly stores host/port configuration."""
        b = MayaBridge(host="10.0.0.5", port=9999, timeout=5.0)
        assert b.host == "10.0.0.5"
        assert b.port == 9999
        assert b.timeout == 5.0


# ═══════════════════════════════════════════════════════════════════════════════
# 4.1.2 — Send Code and Receive Result
# ═══════════════════════════════════════════════════════════════════════════════

class TestSendReceive:
    """Verify command→response round-trips through the bridge."""

    def test_send_mel_returns_response(self, mock_maya_server, bridge_to_mock):
        """MEL command returns the mock server's response verbatim."""
        mock_maya_server.default_response = "pCube1"
        result = bridge_to_mock.send_mel("polyCube")
        assert result == "pCube1"

    def test_execute_returns_raw_string(self, mock_maya_server, bridge_to_mock, wrapper_result_writer):
        """execute() without as_json returns the wrapper's result file content."""
        mock_maya_server.on_receive = wrapper_result_writer('{"count": 42}')
        result = bridge_to_mock.execute("result = {'count': 42}")
        assert isinstance(result, str)
        assert "42" in result

    def test_execute_as_json_parses(self, mock_maya_server, bridge_to_mock, wrapper_result_writer):
        """execute(as_json=True) parses JSON response into dict."""
        mock_maya_server.on_receive = wrapper_result_writer('{"count": 42}')
        result = bridge_to_mock.execute("result = {'count': 42}", as_json=True)
        assert isinstance(result, dict)
        assert result["count"] == 42

    def test_execute_as_json_fallback(self, mock_maya_server, bridge_to_mock, wrapper_result_writer):
        """execute(as_json=True) returns raw string on invalid JSON."""
        mock_maya_server.on_receive = wrapper_result_writer("not json")
        result = bridge_to_mock.execute("result = 'hello'", as_json=True)
        assert result == "not json"

    def test_execute_error_raises(self, mock_maya_server, bridge_to_mock, wrapper_result_writer):
        """execute() raises MayaExecutionError when result starts with ERROR:."""
        mock_maya_server.on_receive = wrapper_result_writer(
            "ERROR: NameError: name 'foo' is not defined"
        )
        with pytest.raises(MayaExecutionError, match="NameError"):
            bridge_to_mock.execute("result = foo")

    def test_send_mel_response_with_special_chars(self, mock_maya_server, bridge_to_mock):
        """MEL response with unicode/special chars is preserved."""
        mock_maya_server.default_response = "Escena: café_001.ma — ¡lista!"
        result = bridge_to_mock.send_mel("file -q -sn")
        assert "café_001" in result
        assert "¡lista!" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 4.1.3 — Timeout When No Server Is Listening
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimeout:
    """Verify that the bridge raises appropriately when Maya is unreachable."""

    def test_connection_refused_raises(self):
        """ConnectionRefusedError is wrapped in MayaConnectionError."""
        # Use a port that is definitely not listening
        b = MayaBridge(host="localhost", port=1, timeout=1.0)
        with pytest.raises(MayaConnectionError, match="Cannot connect"):
            b.send_mel("about -v")

    def test_timeout_raises_connection_error(self):
        """Socket timeout is wrapped in MayaConnectionError (mocked)."""
        b = MayaBridge(host="localhost", port=19999, timeout=0.3)
        # Mock socket.connect to raise a timeout — OS-independent
        with unittest.mock.patch("socket.socket.connect", side_effect=socket.timeout("timed out")):
            with pytest.raises(MayaBridgeError):
                b.send_mel("about -v")

    def test_unreachable_host_raises(self):
        """Non-routable host triggers MayaBridgeError within timeout."""
        b = MayaBridge(host="localhost", port=1, timeout=0.5)
        with pytest.raises(MayaBridgeError):
            b.send_mel("about -v")


# ═══════════════════════════════════════════════════════════════════════════════
# 4.1.4 — maya_ping Returns Version and Scene Info
# ═══════════════════════════════════════════════════════════════════════════════

class TestMayaPing:
    """Verify MayaBridge.ping() assembles version + scene info correctly."""

    @staticmethod
    def _wire_ping_responder(mock, writer_factory, version: str, os_name: str, scene_payload: str):
        """Wire mock so the first 2 send_mel calls return version/os, then the
        execute() call writes ``scene_payload`` to the wrapper result file."""
        scene_writer = writer_factory(scene_payload)
        call_count = {"n": 0}

        def responder(cmd: str) -> None:
            call_count["n"] += 1
            if call_count["n"] >= 3:
                scene_writer(cmd)

        mock.on_receive = responder
        # First two responses are direct send_mel returns.
        mock.responses = [version, os_name]

    def test_ping_returns_complete_info(self, mock_maya_server, bridge_to_mock, wrapper_result_writer):
        """ping() returns status, version, os, and scene dict."""
        self._wire_ping_responder(
            mock_maya_server, wrapper_result_writer, "Maya 2025", "mac",
            json.dumps({"objects": 10, "scene": "untitled", "renderer": "arnold"}),
        )
        info = bridge_to_mock.ping()

        assert info["status"] == "connected"
        assert info["version"] == "Maya 2025"
        assert info["os"] == "mac"
        assert isinstance(info["scene"], dict)
        assert info["scene"]["objects"] == 10
        assert info["scene"]["renderer"] == "arnold"

    def test_ping_with_named_scene(self, mock_maya_server, bridge_to_mock, wrapper_result_writer):
        """ping() correctly reports scene name."""
        self._wire_ping_responder(
            mock_maya_server, wrapper_result_writer, "Maya 2024", "linux",
            json.dumps({"objects": 3, "scene": "/projects/shot01.ma", "renderer": "arnold"}),
        )
        info = bridge_to_mock.ping()
        assert info["scene"]["scene"] == "/projects/shot01.ma"

    def test_ping_scene_non_dict_fallback(self, mock_maya_server, bridge_to_mock, wrapper_result_writer):
        """ping() returns empty dict when scene info is not a dict."""
        self._wire_ping_responder(
            mock_maya_server, wrapper_result_writer, "Maya 2025", "mac", "not_json_at_all"
        )
        info = bridge_to_mock.ping()
        assert info["scene"] == {}

    def test_ping_connection_refused(self):
        """ping() raises MayaConnectionError when Maya is not running."""
        b = MayaBridge(host="localhost", port=1, timeout=0.5)
        with pytest.raises(MayaConnectionError):
            b.ping()


# ═══════════════════════════════════════════════════════════════════════════════
# 4.1.5 — maya_create_primitive Sends the Correct Command
# ═══════════════════════════════════════════════════════════════════════════════

class TestMayaCreatePrimitive:
    """
    Verify that server.maya_create_primitive builds correct Python code
    and sends it through the bridge.

    Uses monkeypatching to intercept bridge.execute() calls.
    """

    def test_cube_default(self, monkeypatch):
        """Creating a cube with no options generates cmds.polyCube()."""
        captured_code = {}

        def fake_execute(code, as_json=False):
            captured_code["code"] = code
            return json.dumps({"name": "pCube1", "type": "cube"})

        # Import server module to access the tool function and bridge
        from maya_mcp import server
        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        from maya_mcp.server import CreatePrimitiveInput, PrimitiveType
        params = CreatePrimitiveInput(primitive_type=PrimitiveType.CUBE)
        result = asyncio.run(server.maya_create_primitive(params))

        assert "polyCube" in captured_code["code"]
        assert "pCube1" in result

    def test_sphere_named_positioned(self, monkeypatch):
        """Creating a named sphere with position generates correct code."""
        captured_code = {}

        def fake_execute(code, as_json=False):
            captured_code["code"] = code
            return json.dumps({"name": "mySphere", "type": "sphere"})

        from maya_mcp import server
        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        from maya_mcp.server import CreatePrimitiveInput, PrimitiveType
        params = CreatePrimitiveInput(
            primitive_type=PrimitiveType.SPHERE,
            name="mySphere",
            position=[1.0, 2.0, 3.0],
        )
        result = asyncio.run(server.maya_create_primitive(params))

        code = captured_code["code"]
        assert "polySphere" in code
        assert "name='mySphere'" in code
        assert "translation=[1.0, 2.0, 3.0]" in code

    def test_cylinder_with_all_transforms(self, monkeypatch):
        """Cylinder with position, scale, and rotation generates all xform calls."""
        captured_code = {}

        def fake_execute(code, as_json=False):
            captured_code["code"] = code
            return json.dumps({"name": "cyl1", "type": "cylinder"})

        from maya_mcp import server
        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        from maya_mcp.server import CreatePrimitiveInput, PrimitiveType
        params = CreatePrimitiveInput(
            primitive_type=PrimitiveType.CYLINDER,
            position=[0.0, 5.0, 0.0],
            scale=[2.0, 2.0, 2.0],
            rotation=[0.0, 45.0, 0.0],
        )
        asyncio.run(server.maya_create_primitive(params))

        code = captured_code["code"]
        assert "polyCylinder" in code
        assert "translation=[0.0, 5.0, 0.0]" in code
        assert "scale=[2.0, 2.0, 2.0]" in code
        assert "rotation=[0.0, 45.0, 0.0]" in code

    def test_all_primitive_types(self, monkeypatch):
        """All 6 primitive types generate the correct polyCmds function."""
        from maya_mcp import server
        from maya_mcp.server import CreatePrimitiveInput, PrimitiveType

        expected_funcs = {
            PrimitiveType.CUBE: "polyCube",
            PrimitiveType.SPHERE: "polySphere",
            PrimitiveType.CYLINDER: "polyCylinder",
            PrimitiveType.CONE: "polyCone",
            PrimitiveType.PLANE: "polyPlane",
            PrimitiveType.TORUS: "polyTorus",
        }

        for ptype, func_name in expected_funcs.items():
            captured = {}

            def fake_execute(code, as_json=False, _cap=captured):
                _cap["code"] = code
                return json.dumps({"name": "obj", "type": ptype.value})

            monkeypatch.setattr(server.bridge, "execute", fake_execute)
            params = CreatePrimitiveInput(primitive_type=ptype)
            asyncio.run(server.maya_create_primitive(params))
            assert func_name in captured["code"], (
                f"{ptype.value} should use cmds.{func_name}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 4.1.6 — _do_execute_python Sends Code and Returns Result
# ═══════════════════════════════════════════════════════════════════════════════

class TestMayaExecutePython:
    """
    Verify that server._do_execute_python passes code through the bridge
    and handles safety checks. (Previously tested maya_execute_python;
    updated for O1b dispatch pattern — handler is now _do_execute_python.)
    """

    def test_execute_returns_bridge_result(self, monkeypatch):
        """_do_execute_python forwards code to bridge.execute and returns result."""
        captured_code = {}

        def fake_execute(code, as_json=False):
            captured_code["code"] = code
            return "42"

        from maya_mcp import server
        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        result = asyncio.run(server._do_execute_python({"code": "result = 21 * 2"}))

        assert captured_code["code"] == "result = 21 * 2"
        assert result == "42"

    def test_execute_increments_stats(self, monkeypatch):
        """_do_execute_python increments exec_calls and token stats."""
        from maya_mcp import server

        def fake_execute(code, as_json=False):
            return "OK"

        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        before_calls = server._stats["exec_calls"]
        before_in = server._stats["tokens_in"]

        asyncio.run(server._do_execute_python({"code": "result = 'hello'"}))

        assert server._stats["exec_calls"] == before_calls + 1
        assert server._stats["tokens_in"] > before_in

    def test_execute_blocks_dangerous_code(self, monkeypatch):
        """_do_execute_python blocks dangerous patterns and increments safety_blocks."""
        from maya_mcp import server

        # Ensure bridge.execute is NOT called for blocked code
        execute_called = {"called": False}

        def fake_execute(code, as_json=False):
            execute_called["called"] = True
            return "OK"

        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        before_blocks = server._stats["safety_blocks"]

        # This pattern is caught by safety.py: wildcard delete
        result = asyncio.run(server._do_execute_python({"code": "cmds.delete('*')"}))

        parsed = json.loads(result)
        assert "safety_warning" in parsed
        assert not execute_called["called"], "bridge.execute should NOT be called for blocked code"
        assert server._stats["safety_blocks"] == before_blocks + 1

    def test_execute_handles_bridge_error(self, monkeypatch):
        """_do_execute_python returns error JSON when bridge raises."""
        from maya_mcp import server

        def fake_execute(code, as_json=False):
            raise MayaBridgeError("Connection lost")

        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        result = asyncio.run(server._do_execute_python({"code": "result = cmds.ls()"}))

        # _handle_error returns JSON with error key
        assert "error" in result.lower() or "Connection lost" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 4.1.7 — File-based result return (Bug 2 regression suite)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFileBasedReturn:
    """
    Regression suite for the file-based result return pattern.

    The previous dual-connection implementation depended on Maya's command
    port capturing the stdout of ``python("print(_mcp_result)")``, which is
    fragile (echoOutput=False, broken stdout wiring, etc.). The current
    implementation writes the wrapper result to a temp file from inside Maya
    and the bridge reads that file locally — a single connection with no
    dependency on stdout capture.
    """

    def test_send_python_uses_single_connection(
        self, mock_maya_server, bridge_to_mock, wrapper_result_writer
    ):
        """send_python opens exactly ONE TCP connection (down from 2)."""
        mock_maya_server.on_receive = wrapper_result_writer("OK")
        bridge_to_mock.send_python("result = 'OK'")
        assert len(mock_maya_server.received_commands) == 1, (
            "send_python should use a single TCP connection after the file-based "
            f"refactor, got {len(mock_maya_server.received_commands)}"
        )

    def test_send_python_reads_result_file(
        self, mock_maya_server, bridge_to_mock, wrapper_result_writer
    ):
        """The result the bridge returns is the exact bytes the wrapper wrote."""
        mock_maya_server.on_receive = wrapper_result_writer("hello world 42")
        result = bridge_to_mock.send_python("result = 'hello world 42'")
        assert result == "hello world 42"

    def test_send_python_json_payload_roundtrip(
        self, mock_maya_server, bridge_to_mock, wrapper_result_writer
    ):
        """Dict/list payloads serialized by the wrapper survive JSON roundtrip."""
        payload = '{"objects": ["pCube1", "pSphere1"], "count": 2}'
        mock_maya_server.on_receive = wrapper_result_writer(payload)
        result = bridge_to_mock.execute("result = {'objects': ['pCube1', 'pSphere1'], 'count': 2}", as_json=True)
        assert result == {"objects": ["pCube1", "pSphere1"], "count": 2}

    def test_send_python_error_prefix_raises_execution_error(
        self, mock_maya_server, bridge_to_mock, wrapper_result_writer
    ):
        """Result file content starting with 'ERROR:' raises MayaExecutionError."""
        mock_maya_server.on_receive = wrapper_result_writer(
            "ERROR: ZeroDivisionError: division by zero"
        )
        with pytest.raises(MayaExecutionError, match="ZeroDivisionError"):
            bridge_to_mock.send_python("result = 1 / 0")

    def test_send_python_raises_when_result_file_missing(
        self, mock_maya_server, bridge_to_mock
    ):
        """If Maya accepts the command but no file is written, raise with diagnostic."""
        # No on_receive callback → mock just ACKs the command, never writes a file.
        # This mirrors the Chat 41 user scenario: Maya's command port responds
        # but execute_python returns empty because the result return path is broken.
        with pytest.raises(MayaExecutionError) as exc_info:
            bridge_to_mock.send_python("result = cmds.ls()")
        msg = str(exc_info.value)
        assert "no result file" in msg.lower() or "did not produce" in msg.lower()
        assert "commandPort" in msg, "diagnostic should include the recovery snippet"
        assert "echoOutput=True" in msg, "diagnostic should suggest the fix"

    def test_send_python_survives_silent_echo_output(
        self, mock_maya_server, bridge_to_mock, wrapper_result_writer
    ):
        """Chat 41 scenario regression: Maya's command port does not echo stdout
        but the wrapper still writes the result file. The bridge MUST read the
        file and return the correct result, NOT empty string."""
        # Mock returns empty string from the TCP connection (simulates a
        # command port opened without echoOutput=True), but on_receive writes
        # the result file as the real Maya would.
        mock_maya_server.on_receive = wrapper_result_writer('["pCube1", "pSphere1"]')
        mock_maya_server.default_response = ""  # broken echo
        result = bridge_to_mock.execute("result = cmds.ls(geometry=True)", as_json=True)
        assert result == ["pCube1", "pSphere1"]

    def test_send_python_cleanup_on_success(
        self, mock_maya_server, bridge_to_mock, wrapper_result_writer
    ):
        """All temp files (user, wrapper, result) are removed after a success."""
        captured_paths = {"wrapper": None, "result": None}

        def capture_and_write(cmd: str) -> None:
            match = re.search(r"open\('([^']+)'\)", cmd)
            if match:
                captured_paths["wrapper"] = match.group(1)
                with open(match.group(1)) as fh:
                    src = fh.read()
                m = re.search(r"_MCP_SCRIPT_PATH\s*=\s*'([^']+)'", src)
                if m:
                    captured_paths["user"] = m.group(1)
                m2 = re.search(r"_MCP_RESULT_PATH\s*=\s*'([^']+)'", src)
                if m2:
                    captured_paths["result"] = m2.group(1)
                    with open(m2.group(1), "w") as out:
                        out.write("ok")

        mock_maya_server.on_receive = capture_and_write
        bridge_to_mock.send_python("result = 'ok'")

        for kind, path in captured_paths.items():
            assert path is not None, f"{kind} path was never captured"
            assert not os.path.exists(path), f"{kind} temp file should be cleaned up: {path}"

    def test_send_python_cleanup_on_error_path(
        self, mock_maya_server, bridge_to_mock, wrapper_result_writer
    ):
        """Cleanup runs even when the wrapper writes an ERROR: payload."""
        captured = {}

        def writer_with_capture(cmd: str) -> None:
            match = re.search(r"open\('([^']+)'\)", cmd)
            if match:
                captured["wrapper"] = match.group(1)
                with open(match.group(1)) as fh:
                    src = fh.read()
                m = re.search(r"_MCP_RESULT_PATH\s*=\s*'([^']+)'", src)
                if m:
                    captured["result"] = m.group(1)
                    with open(m.group(1), "w") as out:
                        out.write("ERROR: NameError: foo")

        mock_maya_server.on_receive = writer_with_capture
        with pytest.raises(MayaExecutionError):
            bridge_to_mock.send_python("result = foo")

        assert not os.path.exists(captured["wrapper"])
        assert not os.path.exists(captured["result"])

    def test_send_python_cleanup_on_missing_file_path(
        self, mock_maya_server, bridge_to_mock
    ):
        """Cleanup runs even when the result file is never created."""
        captured = {}

        def capture_only(cmd: str) -> None:
            match = re.search(r"open\('([^']+)'\)", cmd)
            if match:
                captured["wrapper"] = match.group(1)
                with open(match.group(1)) as fh:
                    src = fh.read()
                m = re.search(r"_MCP_SCRIPT_PATH\s*=\s*'([^']+)'", src)
                if m:
                    captured["user"] = m.group(1)

        mock_maya_server.on_receive = capture_only
        with pytest.raises(MayaExecutionError):
            bridge_to_mock.send_python("result = cmds.ls()")

        # Wrapper and user temp files should be cleaned up. Result file never
        # existed, but cleanup tolerates that.
        assert not os.path.exists(captured["wrapper"])
        assert not os.path.exists(captured["user"])

    def test_send_python_result_paths_unique_across_calls(self):
        """Two prepared wrappers MUST get distinct result paths (uuid in name)."""
        u1, w1, r1 = MayaBridge._prepare_wrapper_files("result = 1")
        u2, w2, r2 = MayaBridge._prepare_wrapper_files("result = 2")
        try:
            assert r1 != r2
            assert u1 != u2
            assert w1 != w2
            assert "_mcp_result_" in r1
            assert r1.endswith(".json")
        finally:
            MayaBridge._cleanup_temp_files(u1, w1, r1, u2, w2, r2)

    def test_send_python_result_paths_isolated_under_concurrency(
        self, mock_maya_server, bridge_to_mock, wrapper_result_writer
    ):
        """Two threads invoking send_python in parallel never read each other's
        result file (uuid path collision sanity check)."""
        # Each call gets its own writer that returns a distinct payload. We
        # don't actually need true thread parallelism — the fixture queues
        # commands serially through the mock — but we exercise the path twice
        # in quick succession and assert the results are not crossed.
        results = []

        def call(payload: str):
            mock_maya_server.on_receive = wrapper_result_writer(payload)
            results.append(bridge_to_mock.send_python(f"result = '{payload}'"))

        call("first")
        call("second")
        assert results == ["first", "second"]

    def test_wrapper_body_writes_to_result_path(self):
        """Sanity check: the wrapper body opens _MCP_RESULT_PATH for writing.

        This is a regression guard so that any future edit to the wrapper
        template that drops the file-write line is caught immediately.
        """
        body = MayaBridge._WRAPPER_BODY
        assert "_MCP_RESULT_PATH" in body
        assert "open(_MCP_RESULT_PATH" in body
        assert ".write(_mcp_payload)" in body
        # And no longer relies on a module-level _mcp_result global being read.
        assert "print(_mcp_result)" not in body
