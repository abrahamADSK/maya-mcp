"""
test_vision3d.py
================
Tests for the 6 Vision3D integration tools in src/maya_mcp/server.py.

Uses httpx MockTransport to simulate the Vision3D REST API — no real
GPU server, network access, or Maya instance required.

Test cases (from TESTING_PLAN section 4.4):
  1. vision3d_health checks server status
  2. shape_generate_remote submits job and returns job_id
  3. shape_generate_text submits text prompt and returns job_id
  4. vision3d_poll returns running status with log lines
  5. vision3d_poll returns completed status with file list
  6. vision3d_download saves files to disk (tmp_path)
  7. Server down returns informative error without crash
"""

import json
from unittest.mock import patch

import httpx
import pytest

# ── Helpers ───────────────────────────────────────────────────────────────

_MOCK_BASE_URL = "http://mock-vision3d:8000"


def _mock_client(handler) -> httpx.AsyncClient:
    """Build a mock httpx.AsyncClient with base_url matching server config."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=_MOCK_BASE_URL,
    )


def _json_response(data: dict, status_code: int = 200) -> httpx.Response:
    """Build an httpx.Response from a dict."""
    return httpx.Response(
        status_code=status_code,
        json=data,
        request=httpx.Request("GET", "http://test"),
    )


def _bytes_response(content: bytes, status_code: int = 200) -> httpx.Response:
    """Build an httpx.Response with raw bytes (for file download)."""
    return httpx.Response(
        status_code=status_code,
        content=content,
        request=httpx.Request("GET", "http://test"),
    )


# ── Import server module ──────────────────────────────────────────────────
# conftest.py installs the mcp SDK stub before any test file is collected,
# so importing maya_mcp.server works without the full MCP SDK.
from maya_mcp import server as srv


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_vision3d_state():
    """Reset all vision3d per-session state before each test.

    The existing tests assume a URL is already selected (they mock the
    HTTP client directly). We pre-seed ``_selected_vision3d`` to the mock
    base URL so ``_resolve_client_or_error`` returns the mocked client
    path instead of ``vision3d_url_required``. Tests that want to exercise
    the unselected path reset ``_selected_vision3d = None`` explicitly.
    """
    srv._selected_vision3d = _MOCK_BASE_URL
    srv._http_clients.clear()
    srv._job_log_cursors.clear()
    yield
    srv._selected_vision3d = None
    srv._http_clients.clear()
    srv._job_log_cursors.clear()


# ── 1. vision3d_health — server available ─────────────────────────────────

class TestVision3dHealth:
    """vision3d_health checks server status (mock HTTP)."""

    @pytest.mark.asyncio
    async def test_health_available(self, mock_ctx):
        """Health returns available=True with GPU info when server responds 200."""
        health_data = {
            "gpu": "NVIDIA RTX 4090",
            "vram_gb": 24,
            "models": ["hunyuan3d-turbo", "hunyuan3d-full"],
            "text_to_3d": "enabled",
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/health"
            return _json_response(health_data)

        mock_client = _mock_client(handler)
        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            result = json.loads(await srv._do_v3d_health({}, mock_ctx))

        assert result["available"] is True
        assert result["gpu"] == "NVIDIA RTX 4090"
        assert result["vram_gb"] == 24
        assert "hunyuan3d-turbo" in result["models"]
        assert result["text_to_3d"] == "enabled"

    @pytest.mark.asyncio
    async def test_health_non_200(self, mock_ctx):
        """Health returns available=False when server responds non-200."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=503,
                text="Service Unavailable",
                request=request,
            )

        mock_client = _mock_client(handler)
        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            result = json.loads(await srv._do_v3d_health({}, mock_ctx))

        assert result["available"] is False
        assert "503" in result["error"]

    @pytest.mark.asyncio
    async def test_health_server_down(self, mock_ctx):
        """Health returns available=False with informative error when server unreachable."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        mock_client = _mock_client(handler)
        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            result = json.loads(await srv._do_v3d_health({}, mock_ctx))

        assert result["available"] is False
        assert "error" in result
        assert "hint" in result


# ── 2. shape_generate_remote — submit image job ──────────────────────────

class TestShapeGenerateRemote:
    """shape_generate_remote submits job and returns job_id."""

    @pytest.mark.asyncio
    async def test_submit_returns_job_id(self, mock_ctx, tmp_path):
        """Successful submission returns status=started and a job_id."""
        # Create a fake image file
        image = tmp_path / "ref.png"
        image.write_bytes(b"\x89PNG fake image data")

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/generate-full"
            return _json_response({"job_id": "job-img-001"})

        mock_client = _mock_client(handler)
        params = srv.ShapeGenerateInput(
            image_path=str(image),
            output_subdir="test_asset",
            preset="medium",
        )

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            result = json.loads(await srv._do_v3d_generate_image(params.model_dump(), mock_ctx))

        assert result["status"] == "started"
        assert result["job_id"] == "job-img-001"
        assert "next_step" in result
        assert "poll" in result["next_step"]

    @pytest.mark.asyncio
    async def test_image_not_found(self, mock_ctx, tmp_path):
        """Returns error when image path does not exist."""
        params = srv.ShapeGenerateInput(
            image_path="/nonexistent/image.png",
            output_subdir="test",
        )

        with patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            result = json.loads(await srv._do_v3d_generate_image(params.model_dump(), mock_ctx))

        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_api_error_returns_hint(self, mock_ctx, tmp_path):
        """Returns error and hint when API responds with non-200."""
        image = tmp_path / "ref.png"
        image.write_bytes(b"\x89PNG fake")

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=500, text="GPU OOM", request=request)

        mock_client = _mock_client(handler)
        params = srv.ShapeGenerateInput(image_path=str(image), output_subdir="t")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            result = json.loads(await srv._do_v3d_generate_image(params.model_dump(), mock_ctx))

        assert "error" in result
        assert "500" in result["error"]
        assert "hint" in result


# ── 3. shape_generate_text — submit text prompt ──────────────────────────

class TestShapeGenerateText:
    """shape_generate_text submits text prompt and returns job_id."""

    @pytest.mark.asyncio
    async def test_text_submit_returns_job_id(self, mock_ctx, tmp_path):
        """Successful text submission returns status=started and a job_id."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/generate-text"
            return _json_response({"job_id": "job-txt-042"})

        mock_client = _mock_client(handler)
        params = srv.ShapeTextInput(
            text_prompt="a small wooden mailbox",
            output_subdir="mailbox_0",
            preset="low",
        )

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            result = json.loads(await srv._do_v3d_generate_text(params.model_dump(), mock_ctx))

        assert result["status"] == "started"
        assert result["job_id"] == "job-txt-042"
        assert "next_step" in result

    @pytest.mark.asyncio
    async def test_text_api_error(self, mock_ctx, tmp_path):
        """Returns error when API responds with non-200."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=422, text="Invalid prompt", request=request)

        mock_client = _mock_client(handler)
        params = srv.ShapeTextInput(text_prompt="x", output_subdir="t")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            result = json.loads(await srv._do_v3d_generate_text(params.model_dump(), mock_ctx))

        assert "error" in result
        assert "422" in result["error"]


# ── 4 & 5. vision3d_poll — running and completed status ──────────────────

class TestVision3dPoll:
    """vision3d_poll returns running/completed status with log lines."""

    @pytest.mark.asyncio
    async def test_poll_running_with_logs(self, mock_ctx):
        """Poll returns status=running with new log lines."""
        job_data = {
            "status": "running",
            "elapsed_s": 12,
            "log": ["Loading model...", "Generating shape (step 5/20)..."],
            "files": [],
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            assert "/api/jobs/job-run-01" in str(request.url)
            return _json_response(job_data)

        mock_client = _mock_client(handler)
        params = srv.Vision3DPollInput(job_id="job-run-01")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            result = json.loads(await srv._do_v3d_poll(params.model_dump(), mock_ctx))

        assert result["status"] == "running"
        assert result["elapsed_s"] == 12
        assert len(result["new_log_lines"]) == 2
        assert "Loading model" in result["new_log_lines"][0]
        assert "next_step" in result
        assert "poll" in result["next_step"]

    @pytest.mark.asyncio
    async def test_poll_incremental_logs(self, mock_ctx):
        """Second poll only returns NEW log lines (incremental delivery)."""
        # Simulate first poll with 2 lines
        srv._job_log_cursors["job-inc-01"] = 2

        job_data = {
            "status": "running",
            "elapsed_s": 30,
            "log": ["step 1", "step 2", "step 3", "step 4"],
            "files": [],
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(job_data)

        mock_client = _mock_client(handler)
        params = srv.Vision3DPollInput(job_id="job-inc-01")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            result = json.loads(await srv._do_v3d_poll(params.model_dump(), mock_ctx))

        # Should only have the 2 new lines (step 3, step 4)
        assert len(result["new_log_lines"]) == 2
        assert result["new_log_lines"][0] == "step 3"
        assert result["total_log_lines"] == 4

    @pytest.mark.asyncio
    async def test_poll_completed_with_files(self, mock_ctx):
        """Poll returns status=completed with file list and cleanup hint."""
        job_data = {
            "status": "completed",
            "elapsed_s": 180,
            "log": ["Done. Generated 3 files."],
            "files": [
                {"name": "mesh.glb", "size_kb": 512},
                {"name": "textured.glb", "size_kb": 1024},
                {"name": "texture_baked.png", "size_kb": 2048},
            ],
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(job_data)

        mock_client = _mock_client(handler)
        params = srv.Vision3DPollInput(job_id="job-done-01")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            result = json.loads(await srv._do_v3d_poll(params.model_dump(), mock_ctx))

        assert result["status"] == "completed"
        assert result["elapsed_s"] == 180
        assert "mesh.glb" in result["files"]
        assert "textured.glb" in result["files"]
        assert "download" in result["next_step"]

    @pytest.mark.asyncio
    async def test_poll_failed_status(self, mock_ctx):
        """Poll returns status=failed with error message."""
        job_data = {
            "status": "failed",
            "elapsed_s": 5,
            "log": ["Error: CUDA out of memory"],
            "error": "CUDA out of memory",
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(job_data)

        mock_client = _mock_client(handler)
        params = srv.Vision3DPollInput(job_id="job-fail-01")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            result = json.loads(await srv._do_v3d_poll(params.model_dump(), mock_ctx))

        assert result["status"] == "failed"
        assert "CUDA" in result["error"]

    @pytest.mark.asyncio
    async def test_poll_job_not_found(self, mock_ctx):
        """Poll returns error when job_id does not exist (404)."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=404, text="Not Found", request=request)

        mock_client = _mock_client(handler)
        params = srv.Vision3DPollInput(job_id="nonexistent-job")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            result = json.loads(await srv._do_v3d_poll(params.model_dump(), mock_ctx))

        assert "error" in result
        assert "not found" in result["error"].lower()


# ── 6. vision3d_download — saves files to disk ──────────────────────────

class TestVision3dDownload:
    """vision3d_download saves files to disk (mock HTTP + tmp_path)."""

    @pytest.mark.asyncio
    async def test_download_files_to_disk(self, mock_ctx, tmp_path):
        """Downloads specified files and writes them to the output directory."""
        file_contents = {
            "textured.glb": b"FAKE_GLB_DATA_textured",
            "mesh.glb": b"FAKE_GLB_DATA_mesh",
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            for fname, content in file_contents.items():
                if fname in path:
                    return _bytes_response(content)
            return httpx.Response(status_code=404, text="Not Found", request=request)

        mock_client = _mock_client(handler)
        params = srv.Vision3DDownloadInput(
            job_id="job-dl-01",
            output_subdir="asset_42",
            files=["textured.glb", "mesh.glb"],
        )

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            result = json.loads(await srv._do_v3d_download(params.model_dump(), mock_ctx))

        assert result["status"] == "ok"
        assert len(result["downloaded"]) == 2
        assert len(result["failed"]) == 0

        # Verify files actually exist on disk
        out_dir = tmp_path / "reference" / "3d_output" / "asset_42"
        assert (out_dir / "textured.glb").exists()
        assert (out_dir / "mesh.glb").exists()
        assert (out_dir / "textured.glb").read_bytes() == b"FAKE_GLB_DATA_textured"

    @pytest.mark.asyncio
    async def test_download_partial_failure(self, mock_ctx, tmp_path):
        """Reports failed files when some downloads return non-200."""

        async def handler(request: httpx.Request) -> httpx.Response:
            if "textured.glb" in str(request.url):
                return _bytes_response(b"FAKE_GLB")
            return httpx.Response(status_code=404, text="Not Found", request=request)

        mock_client = _mock_client(handler)
        params = srv.Vision3DDownloadInput(
            job_id="job-dl-02",
            output_subdir="partial",
            files=["textured.glb", "missing_file.obj"],
        )

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            result = json.loads(await srv._do_v3d_download(params.model_dump(), mock_ctx))

        assert result["status"] == "ok"
        assert len(result["downloaded"]) == 1
        assert result["downloaded"][0]["name"] == "textured.glb"
        assert "missing_file.obj" in result["failed"]

    @pytest.mark.asyncio
    async def test_download_reports_sizes(self, mock_ctx, tmp_path):
        """Downloaded files include size_kb in the response."""
        content = b"x" * 2048  # 2 KB

        async def handler(request: httpx.Request) -> httpx.Response:
            return _bytes_response(content)

        mock_client = _mock_client(handler)
        params = srv.Vision3DDownloadInput(
            job_id="job-dl-03",
            output_subdir="sizes",
            files=["mesh.glb"],
        )

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            result = json.loads(await srv._do_v3d_download(params.model_dump(), mock_ctx))

        assert result["downloaded"][0]["size_kb"] == 2


# ── 7. Server down — errors without crash ────────────────────────────────

class TestServerDown:
    """Server down returns informative error without crash."""

    @pytest.mark.asyncio
    async def test_health_connect_error_no_crash(self, mock_ctx):
        """vision3d_health handles ConnectError gracefully."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        mock_client = _mock_client(handler)
        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            raw = await srv._do_v3d_health({}, mock_ctx)

        result = json.loads(raw)
        assert result["available"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_poll_connect_error_no_crash(self, mock_ctx):
        """vision3d_poll handles ConnectError gracefully."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        mock_client = _mock_client(handler)
        params = srv.Vision3DPollInput(job_id="job-offline")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)):
            raw = await srv._do_v3d_poll(params.model_dump(), mock_ctx)

        result = json.loads(raw)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_download_connect_error_no_crash(self, mock_ctx, tmp_path):
        """vision3d_download handles ConnectError gracefully."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        mock_client = _mock_client(handler)
        params = srv.Vision3DDownloadInput(
            job_id="job-offline",
            output_subdir="off",
            files=["mesh.glb"],
        )

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            raw = await srv._do_v3d_download(params.model_dump(), mock_ctx)

        result = json.loads(raw)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_generate_remote_connect_error_no_crash(self, mock_ctx, tmp_path):
        """shape_generate_remote handles ConnectError gracefully."""
        image = tmp_path / "img.png"
        image.write_bytes(b"\x89PNG")

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        mock_client = _mock_client(handler)
        params = srv.ShapeGenerateInput(image_path=str(image), output_subdir="off")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            raw = await srv._do_v3d_generate_image(params.model_dump(), mock_ctx)

        result = json.loads(raw)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_generate_text_connect_error_no_crash(self, mock_ctx, tmp_path):
        """shape_generate_text handles ConnectError gracefully."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        mock_client = _mock_client(handler)
        params = srv.ShapeTextInput(text_prompt="a chair", output_subdir="off")

        with patch.object(srv, "_resolve_client_or_error", return_value=(mock_client, None)), \
             patch.object(srv, "_MAC_BASE_DIR", str(tmp_path)):
            raw = await srv._do_v3d_generate_text(params.model_dump(), mock_ctx)

        result = json.loads(raw)
        assert "error" in result


# ── 8. Vision3D per-session URL — freeform, no pool, no config ───────────

class TestVision3dUrlSelection:
    """Per-session, runtime-only URL flow. No config pool, no whitelist."""

    # ── _is_valid_http_url ──────────────────────────────────────────────

    def test_is_valid_http_url_accepts_http(self):
        assert srv._is_valid_http_url("http://example.com:8000") is True

    def test_is_valid_http_url_accepts_https(self):
        assert srv._is_valid_http_url("https://example.com") is True

    def test_is_valid_http_url_accepts_localhost(self):
        # Localhost is a valid URL even though we never fabricate it as a default
        assert srv._is_valid_http_url("http://localhost:8000") is True

    def test_is_valid_http_url_rejects_empty(self):
        assert srv._is_valid_http_url("") is False

    def test_is_valid_http_url_rejects_non_http_scheme(self):
        assert srv._is_valid_http_url("ftp://example.com") is False

    def test_is_valid_http_url_rejects_missing_scheme(self):
        assert srv._is_valid_http_url("example.com:8000") is False

    def test_is_valid_http_url_rejects_missing_host(self):
        assert srv._is_valid_http_url("http://") is False

    def test_is_valid_http_url_rejects_garbage(self):
        assert srv._is_valid_http_url("not a url at all") is False

    # ── _resolve_client_or_error ────────────────────────────────────────

    def test_resolve_client_unselected_returns_url_required(self, monkeypatch):
        """With no selection, resolver returns vision3d_url_required."""
        monkeypatch.setattr(srv, "_selected_vision3d", None)
        monkeypatch.setattr(srv, "_http_clients", {})
        monkeypatch.delenv("GPU_API_URL", raising=False)

        client, err = srv._resolve_client_or_error()

        assert client is None
        parsed = json.loads(err)
        assert parsed["error"] == "vision3d_url_required"
        assert "select_server" in parsed["hint"]
        # No persistent list — the payload does NOT expose a pool
        assert "available" not in parsed
        assert "suggested_default" not in parsed

    def test_resolve_client_unselected_surfaces_env_suggestion(self, monkeypatch):
        """If GPU_API_URL is set, it is surfaced as a suggestion (not auto-selected)."""
        monkeypatch.setattr(srv, "_selected_vision3d", None)
        monkeypatch.setattr(srv, "_http_clients", {})
        monkeypatch.setenv("GPU_API_URL", "http://env-host:8000/")

        client, err = srv._resolve_client_or_error()

        assert client is None
        parsed = json.loads(err)
        assert parsed["error"] == "vision3d_url_required"
        assert parsed["suggested_default"] == "http://env-host:8000"
        # And the selection state is NOT mutated by the suggestion
        assert srv._selected_vision3d is None

    def test_resolve_client_empty_env_is_no_suggestion(self, monkeypatch):
        """Whitespace GPU_API_URL does not count as a suggestion."""
        monkeypatch.setattr(srv, "_selected_vision3d", None)
        monkeypatch.setattr(srv, "_http_clients", {})
        monkeypatch.setenv("GPU_API_URL", "   ")

        client, err = srv._resolve_client_or_error()

        parsed = json.loads(err)
        assert parsed["error"] == "vision3d_url_required"
        assert "suggested_default" not in parsed

    def test_resolve_client_selected_returns_client(self, monkeypatch):
        """With a selection, resolver returns (client, None) and caches it."""
        monkeypatch.setattr(srv, "_selected_vision3d", "http://chosen:8000")
        monkeypatch.setattr(srv, "_http_clients", {})

        client, err = srv._resolve_client_or_error()

        assert err is None
        assert client is not None
        # Same client is cached and returned on the second call
        client2, _ = srv._resolve_client_or_error()
        assert client2 is client

    def test_resolve_client_switch_creates_new_client(self, monkeypatch):
        """Switching _selected_vision3d returns a different cached client."""
        monkeypatch.setattr(srv, "_selected_vision3d", "http://first:8000")
        monkeypatch.setattr(srv, "_http_clients", {})

        client_a, _ = srv._resolve_client_or_error()

        srv._selected_vision3d = "http://second:8000"
        client_b, _ = srv._resolve_client_or_error()

        assert client_a is not client_b
        assert str(client_a.base_url).rstrip("/") == "http://first:8000"
        assert str(client_b.base_url).rstrip("/") == "http://second:8000"

    # ── select_server action ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_server_accepts_any_valid_url(self, mock_ctx, monkeypatch):
        """select_server accepts any valid http URL — no whitelist."""
        monkeypatch.setattr(srv, "_selected_vision3d", None)
        monkeypatch.setattr(srv, "_http_clients", {})

        result = json.loads(
            await srv._do_v3d_select_server(
                {"url": "http://some-user-host:8000"}, mock_ctx
            )
        )

        assert result["status"] == "selected"
        assert result["url"] == "http://some-user-host:8000"
        assert srv._selected_vision3d == "http://some-user-host:8000"

    @pytest.mark.asyncio
    async def test_select_server_accepts_https(self, mock_ctx, monkeypatch):
        monkeypatch.setattr(srv, "_selected_vision3d", None)

        result = json.loads(
            await srv._do_v3d_select_server(
                {"url": "https://secure-host.example.com"}, mock_ctx
            )
        )

        assert result["status"] == "selected"
        assert srv._selected_vision3d == "https://secure-host.example.com"

    @pytest.mark.asyncio
    async def test_select_server_normalises_trailing_slash(self, mock_ctx, monkeypatch):
        monkeypatch.setattr(srv, "_selected_vision3d", None)

        result = json.loads(
            await srv._do_v3d_select_server(
                {"url": "http://some-host:8000/"}, mock_ctx
            )
        )

        assert result["status"] == "selected"
        assert srv._selected_vision3d == "http://some-host:8000"

    @pytest.mark.asyncio
    async def test_select_server_rejects_malformed_url(self, mock_ctx, monkeypatch):
        """Malformed / non-http URLs are rejected; selection unchanged."""
        monkeypatch.setattr(srv, "_selected_vision3d", None)

        result = json.loads(
            await srv._do_v3d_select_server({"url": "not a real url"}, mock_ctx)
        )

        assert "error" in result
        assert "Invalid URL" in result["error"]
        assert srv._selected_vision3d is None

    @pytest.mark.asyncio
    async def test_select_server_rejects_ftp_scheme(self, mock_ctx, monkeypatch):
        monkeypatch.setattr(srv, "_selected_vision3d", None)

        result = json.loads(
            await srv._do_v3d_select_server(
                {"url": "ftp://file-host:21"}, mock_ctx
            )
        )

        assert "error" in result
        assert srv._selected_vision3d is None

    @pytest.mark.asyncio
    async def test_select_server_missing_url_param(self, mock_ctx, monkeypatch):
        monkeypatch.setattr(srv, "_selected_vision3d", None)

        result = json.loads(await srv._do_v3d_select_server({}, mock_ctx))

        assert "error" in result
        assert "Missing required param" in result["error"]
        assert srv._selected_vision3d is None

    # ── end-to-end: unselected action returns vision3d_url_required ─────

    @pytest.mark.asyncio
    async def test_unselected_health_returns_url_required(self, mock_ctx, monkeypatch):
        monkeypatch.setattr(srv, "_selected_vision3d", None)
        monkeypatch.setattr(srv, "_http_clients", {})
        monkeypatch.delenv("GPU_API_URL", raising=False)

        result = json.loads(await srv._do_v3d_health({}, mock_ctx))

        assert result["error"] == "vision3d_url_required"

    @pytest.mark.asyncio
    async def test_unselected_generate_image_returns_url_required(
        self, mock_ctx, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(srv, "_selected_vision3d", None)
        monkeypatch.setattr(srv, "_http_clients", {})
        monkeypatch.delenv("GPU_API_URL", raising=False)

        image = tmp_path / "ref.png"
        image.write_bytes(b"\x89PNG fake")
        params = srv.ShapeGenerateInput(image_path=str(image), output_subdir="t")

        result = json.loads(
            await srv._do_v3d_generate_image(params.model_dump(), mock_ctx)
        )

        assert result["error"] == "vision3d_url_required"

    @pytest.mark.asyncio
    async def test_select_then_health_uses_selected_url(self, mock_ctx, monkeypatch):
        """After select_server, health reaches the selected URL."""
        url = "http://user-typed-host:8000"
        monkeypatch.setattr(srv, "_selected_vision3d", None)
        monkeypatch.setattr(srv, "_http_clients", {})

        sel = json.loads(
            await srv._do_v3d_select_server({"url": url}, mock_ctx)
        )
        assert sel["status"] == "selected"
        assert srv._selected_vision3d == url

        async def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({
                "gpu": "MPS",
                "vram_gb": 48,
                "models": ["fast"],
                "text_to_3d": "disabled",
            })

        mock_client = _mock_client(handler)
        with patch.object(
            srv, "_resolve_client_or_error", return_value=(mock_client, None)
        ):
            health = json.loads(await srv._do_v3d_health({}, mock_ctx))

        assert health["available"] is True
        assert health["gpu"] == "MPS"
        assert health["url"] == url
