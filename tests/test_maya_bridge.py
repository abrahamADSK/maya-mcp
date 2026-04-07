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
import socket
import unittest.mock
import time

import pytest

from maya_bridge import MayaBridge, MayaBridgeError, MayaConnectionError, MayaExecutionError


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

    def test_execute_returns_raw_string(self, mock_maya_server, bridge_to_mock):
        """execute() without as_json returns a string."""
        # send_python does 2 connections: execute + read result
        mock_maya_server.responses = ["", '{"count": 42}']
        result = bridge_to_mock.execute("result = {'count': 42}")
        assert isinstance(result, str)
        assert "42" in result

    def test_execute_as_json_parses(self, mock_maya_server, bridge_to_mock):
        """execute(as_json=True) parses JSON response into dict."""
        mock_maya_server.responses = ["", '{"count": 42}']
        result = bridge_to_mock.execute("result = {'count': 42}", as_json=True)
        assert isinstance(result, dict)
        assert result["count"] == 42

    def test_execute_as_json_fallback(self, mock_maya_server, bridge_to_mock):
        """execute(as_json=True) returns raw string on invalid JSON."""
        mock_maya_server.responses = ["", "not json"]
        result = bridge_to_mock.execute("result = 'hello'", as_json=True)
        assert result == "not json"

    def test_execute_error_raises(self, mock_maya_server, bridge_to_mock):
        """execute() raises MayaExecutionError when result starts with ERROR:."""
        mock_maya_server.responses = ["", "ERROR: NameError: name 'foo' is not defined"]
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

    def test_ping_returns_complete_info(self, mock_maya_server, bridge_to_mock):
        """ping() returns status, version, os, and scene dict."""
        mock_maya_server.responses = [
            # 1st call: send_mel("about -v")
            "Maya 2025",
            # 2nd call: send_mel("about -os")
            "mac",
            # 3rd+4th calls: execute() → send_python does 2 TCP connections
            "",  # connection 1 of send_python (execute wrapper)
            json.dumps({"objects": 10, "scene": "untitled", "renderer": "arnold"}),
        ]
        info = bridge_to_mock.ping()

        assert info["status"] == "connected"
        assert info["version"] == "Maya 2025"
        assert info["os"] == "mac"
        assert isinstance(info["scene"], dict)
        assert info["scene"]["objects"] == 10
        assert info["scene"]["renderer"] == "arnold"

    def test_ping_with_named_scene(self, mock_maya_server, bridge_to_mock):
        """ping() correctly reports scene name."""
        mock_maya_server.responses = [
            "Maya 2024",
            "linux",
            "",
            json.dumps({"objects": 3, "scene": "/projects/shot01.ma", "renderer": "arnold"}),
        ]
        info = bridge_to_mock.ping()
        assert info["scene"]["scene"] == "/projects/shot01.ma"

    def test_ping_scene_non_dict_fallback(self, mock_maya_server, bridge_to_mock):
        """ping() returns empty dict when scene info is not a dict."""
        mock_maya_server.responses = [
            "Maya 2025",
            "mac",
            "",
            "not_json_at_all",
        ]
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

        # We import server module to access the tool function and bridge
        import server
        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        from server import CreatePrimitiveInput, PrimitiveType
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

        import server
        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        from server import CreatePrimitiveInput, PrimitiveType
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

        import server
        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        from server import CreatePrimitiveInput, PrimitiveType
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
        import server
        from server import CreatePrimitiveInput, PrimitiveType

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

        import server
        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        result = asyncio.run(server._do_execute_python({"code": "result = 21 * 2"}))

        assert captured_code["code"] == "result = 21 * 2"
        assert result == "42"

    def test_execute_increments_stats(self, monkeypatch):
        """_do_execute_python increments exec_calls and token stats."""
        import server

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
        import server

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
        import server

        def fake_execute(code, as_json=False):
            raise MayaBridgeError("Connection lost")

        monkeypatch.setattr(server.bridge, "execute", fake_execute)

        result = asyncio.run(server._do_execute_python({"code": "result = cmds.ls()"}))

        # _handle_error returns JSON with error key
        assert "error" in result.lower() or "Connection lost" in result
