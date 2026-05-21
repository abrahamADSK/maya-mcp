"""Tests for the ``ollama_mac`` preflight helper in claude_worker (F1b keep_alive knob).

Background
----------
Ollama's Anthropic-compatible ``/v1/messages`` endpoint silently ignores the
Modelfile ``num_ctx`` directive and falls back to 4096 tokens, which truncates
MCP prompts mid-stream without any error. The console worker warms up the
model on ``/api/generate`` with the desired context window BEFORE spawning
the ``claude`` subprocess to force the larger context.

F1b extends the preflight: ``keep_alive`` is now a config knob
(``ollama_keep_alive`` in ``config.json``) defaulting to ``"30m"`` so
5-15 min reading/typing gaps don't trigger a cold model reload. The
old hard-coded ``"10m"`` is retired.

These tests verify:
  1. The forced context window constant is 8192 (tuned for 4B/9B Mac models).
  2. The helper POSTs to ``{url}/api/generate`` with the expected payload.
  3. The helper is non-fatal: network failures are swallowed, never raised
     up to the Qt worker thread.
  4. resolve_keep_alive reads the knob from config.json, defaults to "30m",
     and rejects non-str/int types.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── PySide stub ───────────────────────────────────────────────────────────────
# console.qt_compat tries PySide6 then PySide2; both are absent in CI/headless
# test environments. Install a minimal stub BEFORE importing claude_worker so
# the module-level `from .qt_compat import QtCore` succeeds. The preflight
# helper we exercise here is plain-Python — it does not touch Qt at runtime.

if "PySide6" not in sys.modules and "PySide2" not in sys.modules:
    _pyside6 = types.ModuleType("PySide6")
    _qtcore = types.ModuleType("PySide6.QtCore")
    _qtwidgets = types.ModuleType("PySide6.QtWidgets")
    _qtgui = types.ModuleType("PySide6.QtGui")

    class _QThreadStub:
        def __init__(self, *a, **kw) -> None: ...
        def start(self) -> None: ...

    class _SignalStub:
        def __init__(self, *a, **kw) -> None: ...
        def connect(self, *a, **kw) -> None: ...
        def emit(self, *a, **kw) -> None: ...

    _qtcore.QThread = _QThreadStub
    _qtcore.Signal = _SignalStub
    _pyside6.QtCore = _qtcore
    _pyside6.QtWidgets = _qtwidgets
    _pyside6.QtGui = _qtgui
    sys.modules["PySide6"] = _pyside6
    sys.modules["PySide6.QtCore"] = _qtcore
    sys.modules["PySide6.QtWidgets"] = _qtwidgets
    sys.modules["PySide6.QtGui"] = _qtgui

    _shiboken6 = types.ModuleType("shiboken6")
    _shiboken6.wrapInstance = lambda *a, **kw: None
    sys.modules["shiboken6"] = _shiboken6

from console import claude_worker  # noqa: E402
from console.claude_worker import resolve_keep_alive  # noqa: E402


def test_ollama_mac_num_ctx_is_8192() -> None:
    """Constant must stay at 8192 (Mac 24 GB unified-memory budget).

    If this value changes, the invariant `ollama_preflight_parity` and the
    CHANGELOG entry should be revisited together.
    """
    assert claude_worker.OLLAMA_MAC_NUM_CTX == 8192


def test_preload_invokes_urlopen_with_expected_url_and_payload() -> None:
    """Helper must POST to /api/generate with num_ctx, keep_alive, stream=False."""
    fake_resp = MagicMock()
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.read = MagicMock(return_value=b"")

    with patch.object(
        claude_worker.urllib.request,
        "urlopen",
        return_value=fake_resp,
    ) as mock_urlopen:
        claude_worker._preload_ollama_mac_model(
            model="qwen3.5-mcp",
            url="http://localhost:11434",
            num_ctx=8192,
            keep_alive="30m",  # F1b: pass resolved value (default "30m")
        )

    assert mock_urlopen.call_count == 1
    req = mock_urlopen.call_args.args[0]
    # urllib.request.Request object — inspect URL + payload
    assert req.full_url == "http://localhost:11434/api/generate"
    assert req.get_method() == "POST"
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["model"] == "qwen3.5-mcp"
    assert payload["prompt"] == ""
    assert payload["stream"] is False
    # keep_alive is now a caller-supplied parameter (F1b); the test passes
    # the default value explicitly to verify it flows through correctly.
    assert payload["keep_alive"] == "30m"
    assert payload["options"] == {"num_ctx": 8192}
    # timeout kwarg must be forwarded so a stalled Ollama never hangs the UI
    assert mock_urlopen.call_args.kwargs.get("timeout") == 120


def test_preload_strips_trailing_slash_from_url() -> None:
    """URL with trailing slash must still produce a single /api/generate path."""
    fake_resp = MagicMock()
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.read = MagicMock(return_value=b"")

    with patch.object(
        claude_worker.urllib.request,
        "urlopen",
        return_value=fake_resp,
    ) as mock_urlopen:
        claude_worker._preload_ollama_mac_model(
            model="qwen3.5:4b",
            url="http://localhost:11434/",
            num_ctx=4096,
        )

    req = mock_urlopen.call_args.args[0]
    assert req.full_url == "http://localhost:11434/api/generate"


def test_preload_is_non_fatal_on_network_error() -> None:
    """If urlopen raises, the helper must return None without propagating."""
    import urllib.error

    with patch.object(
        claude_worker.urllib.request,
        "urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        # Must not raise
        result = claude_worker._preload_ollama_mac_model(
            model="qwen3.5-mcp",
            url="http://localhost:11434",
            num_ctx=8192,
        )
    assert result is None


def test_preload_is_non_fatal_on_generic_exception() -> None:
    """Even unexpected exceptions (timeout, DNS, socket) must be swallowed.

    The preflight is an optimisation — failure must never block a subprocess
    spawn, so we catch the broadest Exception in the helper.
    """
    with patch.object(
        claude_worker.urllib.request,
        "urlopen",
        side_effect=TimeoutError("read timed out"),
    ):
        result = claude_worker._preload_ollama_mac_model(
            model="qwen3.5-mcp",
            url="http://localhost:11434",
            num_ctx=8192,
        )
    assert result is None


# ---------------------------------------------------------------------------
# F1b: resolve_keep_alive knob behaviour
# ---------------------------------------------------------------------------

def test_resolve_keep_alive_default_when_no_config() -> None:
    """When config.json does not exist, resolve_keep_alive returns '30m'."""
    result = resolve_keep_alive(config_path="/nonexistent/path/config.json")
    assert result == "30m"


def test_resolve_keep_alive_reads_string_from_config() -> None:
    """A string value in config.json is returned as-is."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as tmp:
        json.dump({"ollama_keep_alive": "1h"}, tmp)
        tmp_path = tmp.name
    assert resolve_keep_alive(config_path=tmp_path) == "1h"


def test_resolve_keep_alive_reads_int_from_config() -> None:
    """An integer value (seconds) in config.json is returned as int."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as tmp:
        json.dump({"ollama_keep_alive": 3600}, tmp)
        tmp_path = tmp.name
    assert resolve_keep_alive(config_path=tmp_path) == 3600


def test_resolve_keep_alive_rejects_invalid_types() -> None:
    """Non-str/int types (dict, list, None, bool) collapse to the default."""
    for bad_value in [None, True, False, [], {"k": "v"}]:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as tmp:
            json.dump({"ollama_keep_alive": bad_value}, tmp)
            tmp_path = tmp.name
        result = resolve_keep_alive(config_path=tmp_path)
        assert result == "30m", (
            f"Expected '30m' for bad_value={bad_value!r}, got {result!r}"
        )


def test_resolve_keep_alive_absent_key_returns_default() -> None:
    """When ollama_keep_alive key is absent, '30m' is returned."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as tmp:
        json.dump({"backend": "anthropic"}, tmp)
        tmp_path = tmp.name
    assert resolve_keep_alive(config_path=tmp_path) == "30m"


def test_preload_keep_alive_flows_into_payload() -> None:
    """The keep_alive parameter supplied to _preload_ollama_mac_model
    reaches the /api/generate request body unchanged (F1b wire test).
    """
    captured: dict = {}
    fake_resp = MagicMock()
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.read = MagicMock(return_value=b"")

    with patch.object(
        claude_worker.urllib.request,
        "urlopen",
        return_value=fake_resp,
    ) as mock_urlopen:
        claude_worker._preload_ollama_mac_model(
            model="qwen3.5-mcp",
            url="http://localhost:11434",
            num_ctx=8192,
            keep_alive="45m",  # non-default to verify the parameter flows through
        )

    req = mock_urlopen.call_args.args[0]
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["keep_alive"] == "45m"
