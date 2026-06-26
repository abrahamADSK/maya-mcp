"""
test_worldlabs_client.py
========================
Tests for the World Labs (Marble) connector client + Pydantic models in
``src/maya_mcp/worldlabs/``.

ALL HTTP is mocked with ``httpx.MockTransport`` (the same approach as
``test_vision3d.py``) — no real network calls, no credits spent, no app
interaction. ``respx`` is intentionally NOT used (not a project dependency).

Coverage
--------
- ``WLT-Api-Key`` header present on every API call (and absent on signed URLs)
- upload flow: prepare_upload -> PUT -> media_asset_id
- generate happy path returns operation_id (URI and local-file-upload variants)
- generate REFUSES without confirm=True (cost guardrail) and proceeds with it
- poll + wait loop until done
- download_assets writes the expected files (and skips missing assets)
- 4xx / 5xx error handling
- missing API key handling
- credit balance endpoint
- log_generation emits a record
- model validation rejects extra keys (extra="forbid") + discriminated union
"""

from __future__ import annotations

import logging

import httpx
import pytest

from maya_mcp.worldlabs import (
    GenerateRequest,
    GenerationNotConfirmedError,
    ImageryAssets,
    ImagePromptMediaAsset,
    ImagePromptUri,
    MeshAssets,
    MissingAPIKeyError,
    Operation,
    SplatAssets,
    World,
    WorldAssets,
    WorldLabsAPIError,
    WorldLabsClient,
    WorldPrompt,
)

# ── Constants ──────────────────────────────────────────────────────────────

API_HOST = "api.worldlabs.ai"
UPLOAD_URL = "https://signed-upload.example.com/put/abc123"
SPLAT_FULL_URL = "https://cdn.example.com/world/full_res.spz"
SPLAT_100K_URL = "https://cdn.example.com/world/100k.spz"
PANO_URL = "https://cdn.example.com/world/pano.png"
MESH_URL = "https://cdn.example.com/world/collider.glb"


# ── Helpers ────────────────────────────────────────────────────────────────


def _json(data: dict, status: int = 200, request: httpx.Request | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=data,
        request=request or httpx.Request("GET", "http://test"),
    )


def _bytes(content: bytes, status: int = 200, request: httpx.Request | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=content,
        request=request or httpx.Request("GET", "http://test"),
    )


def _client(handler, **kw) -> WorldLabsClient:
    """Build a client whose API + signed-URL traffic both hit ``handler``."""
    return WorldLabsClient(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        **kw,
    )


# ── 1. Auth header present on every API call ───────────────────────────────


class TestAuthHeader:
    def test_auth_header_on_every_api_call(self):
        seen: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == API_HOST:
                seen.append((request.url.path, request.headers.get("WLT-Api-Key")))
                return _json({"operation_id": "op-1", "done": False}, request=request)
            return _bytes(b"", request=request)

        cli = _client(handler)
        cli.generate("https://example.com/img.png", model="marble-1.1", confirm=True)

        assert seen, "no API request was made"
        for path, key in seen:
            assert key == "test-key", f"missing WLT-Api-Key on {path}"

    def test_signed_url_put_has_no_api_key(self, tmp_path):
        """Signed-URL upload must NOT carry the WLT-Api-Key header."""
        img = tmp_path / "ref.png"
        img.write_bytes(b"\x89PNG fake")
        put_keys: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == API_HOST:
                return _json(
                    {
                        "media_asset": {"media_asset_id": "ma-1"},
                        "upload_info": {"upload_url": UPLOAD_URL, "upload_method": "PUT"},
                    },
                    request=request,
                )
            put_keys.append(request.headers.get("WLT-Api-Key"))
            return _bytes(b"", request=request)

        cli = _client(handler)
        cli.upload_image(str(img))
        assert put_keys == [None]


# ── 2. Upload flow ─────────────────────────────────────────────────────────


class TestUploadImage:
    def test_prepare_then_put_returns_media_asset_id(self, tmp_path):
        img = tmp_path / "ref.png"
        img.write_bytes(b"\x89PNG payload-bytes")
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.url.host == API_HOST:
                assert request.url.path == "/marble/v1/media-assets:prepare_upload"
                return _json(
                    {
                        "media_asset": {"media_asset_id": "ma-42"},
                        "upload_info": {
                            "upload_url": UPLOAD_URL,
                            "upload_method": "PUT",
                            "required_headers": {"Content-Type": "image/png"},
                        },
                    },
                    request=request,
                )
            # signed PUT: body must be the file bytes
            assert request.method == "PUT"
            assert request.content == b"\x89PNG payload-bytes"
            return _bytes(b"", request=request)

        cli = _client(handler)
        result = cli.upload_image(str(img))

        assert result == "ma-42"
        assert ("POST", "/marble/v1/media-assets:prepare_upload") in calls
        assert any(method == "PUT" for method, _ in calls)

    def test_upload_missing_file_raises(self):
        cli = _client(lambda r: _bytes(b""))
        with pytest.raises(FileNotFoundError):
            cli.upload_image("/nonexistent/does-not-exist.png")

    def test_prepare_upload_missing_fields_raises(self, tmp_path):
        img = tmp_path / "ref.png"
        img.write_bytes(b"x")

        def handler(request: httpx.Request) -> httpx.Response:
            return _json({"media_asset": {}, "upload_info": {}}, request=request)

        cli = _client(handler)
        with pytest.raises(WorldLabsAPIError):
            cli.upload_image(str(img))


# ── 3. Generate — guardrail + happy paths ──────────────────────────────────


class TestGenerateGuardrail:
    def test_refuses_without_confirm(self):
        """Cost guardrail: no confirm -> raise, and NO HTTP is issued."""

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("network must not be touched without confirm=True")

        cli = _client(handler)
        with pytest.raises(GenerationNotConfirmedError) as exc:
            cli.generate("https://example.com/x.png", model="marble-1.1-plus")

        assert exc.value.model == "marble-1.1-plus"
        assert exc.value.approx_credits == 3000
        msg = str(exc.value)
        assert "marble-1.1-plus" in msg
        assert "credit" in msg.lower()
        assert "confirm=True" in msg

    def test_proceeds_with_confirm_uri(self):
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json_mod

            assert request.url.path == "/marble/v1/worlds:generate"
            bodies.append(_json_mod.loads(request.content.decode()))
            return _json({"operation_id": "op-uri-7", "done": False}, request=request)

        cli = _client(handler)
        op_id = cli.generate(
            "https://example.com/img.png",
            model="marble-1.1",
            display_name="My World",
            text_prompt="sunny meadow",
            confirm=True,
        )

        assert op_id == "op-uri-7"
        body = bodies[0]
        assert body["model"] == "marble-1.1"
        assert body["display_name"] == "My World"
        assert body["world_prompt"]["type"] == "image"
        assert body["world_prompt"]["image_prompt"]["source"] == "uri"
        assert body["world_prompt"]["image_prompt"]["uri"] == "https://example.com/img.png"
        assert body["world_prompt"]["text_prompt"] == "sunny meadow"

    def test_proceeds_with_confirm_local_file_uploads_first(self, tmp_path):
        img = tmp_path / "ref.png"
        img.write_bytes(b"\x89PNG local")
        order: list[str] = []
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json_mod

            if request.url.host == API_HOST:
                if request.url.path.endswith("prepare_upload"):
                    order.append("prepare")
                    return _json(
                        {
                            "media_asset": {"media_asset_id": "ma-99"},
                            "upload_info": {"upload_url": UPLOAD_URL, "upload_method": "PUT"},
                        },
                        request=request,
                    )
                order.append("generate")
                bodies.append(_json_mod.loads(request.content.decode()))
                return _json({"operation_id": "op-file-3", "done": False}, request=request)
            order.append("put")
            return _bytes(b"", request=request)

        cli = _client(handler)
        op_id = cli.generate(str(img), model="marble-1.1", confirm=True)

        assert op_id == "op-file-3"
        assert order == ["prepare", "put", "generate"]
        ip = bodies[0]["world_prompt"]["image_prompt"]
        assert ip["source"] == "media_asset"
        assert ip["media_asset_id"] == "ma-99"

    def test_log_generation_emitted(self, caplog):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json({"operation_id": "op-log-1", "done": False}, request=request)

        cli = _client(handler)
        with caplog.at_level(logging.INFO, logger="maya_mcp.worldlabs"):
            cli.generate("https://example.com/x.png", confirm=True)

        records = [r.getMessage() for r in caplog.records]
        assert any("world generation confirmed" in m and "op-log-1" in m for m in records)


# ── 4. Poll + wait ─────────────────────────────────────────────────────────


class TestPollWait:
    def test_poll_returns_operation(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/marble/v1/operations/op-7"
            return _json({"operation_id": "op-7", "done": False}, request=request)

        cli = _client(handler)
        op = cli.poll("op-7")
        assert isinstance(op, Operation)
        assert op.operation_id == "op-7"
        assert op.done is False

    def test_wait_loops_until_done(self):
        state = {"calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["calls"] += 1
            if state["calls"] < 3:
                return _json({"operation_id": "op-w", "done": False}, request=request)
            return _json(
                {
                    "operation_id": "op-w",
                    "done": True,
                    "response": {
                        "world_id": "w-1",
                        "assets": {"splats": {"spz_urls": {"full_res": SPLAT_FULL_URL}}},
                    },
                },
                request=request,
            )

        seen_status: list[bool] = []
        cli = _client(handler)
        op = cli.wait("op-w", interval=0, timeout=30, on_status=lambda o: seen_status.append(o.done))

        assert op.done is True
        assert op.response is not None
        assert op.response.world_id == "w-1"
        assert state["calls"] == 3
        assert seen_status == [False, False, True]

    def test_wait_times_out(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json({"operation_id": "op-t", "done": False}, request=request)

        cli = _client(handler)
        with pytest.raises(TimeoutError):
            cli.wait("op-t", interval=0, timeout=-1)


# ── 5. Download ────────────────────────────────────────────────────────────


def _world_with_all_assets() -> World:
    return World(
        world_id="w-dl",
        assets=WorldAssets(
            splats=SplatAssets(spz_urls={"full_res": SPLAT_FULL_URL, "100k": SPLAT_100K_URL}),
            imagery=ImageryAssets(pano_url=PANO_URL),
            mesh=MeshAssets(collider_mesh_url=MESH_URL),
        ),
    )


class TestDownloadAssets:
    def test_download_writes_expected_files(self, tmp_path):
        payloads = {
            SPLAT_FULL_URL: b"SPZ_FULL_RES_BYTES",
            PANO_URL: b"PANO_PNG_BYTES",
            MESH_URL: b"GLB_MESH_BYTES",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            for known, content in payloads.items():
                if url == known:
                    return _bytes(content, request=request)
            return _bytes(b"", status=404, request=request)

        cli = _client(handler)
        out = cli.download_assets(_world_with_all_assets(), tmp_path)

        assert set(out.keys()) == {"splats_full_res", "pano", "mesh"}
        assert out["splats_full_res"] == tmp_path / "splats_full_res.spz"
        assert (tmp_path / "splats_full_res.spz").read_bytes() == b"SPZ_FULL_RES_BYTES"
        assert (tmp_path / "pano.png").read_bytes() == b"PANO_PNG_BYTES"
        assert (tmp_path / "collider_mesh.glb").read_bytes() == b"GLB_MESH_BYTES"

    def test_download_accepts_done_operation(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return _bytes(b"SPZ100K", request=request)

        op = Operation(operation_id="op-x", done=True, response=_world_with_all_assets())
        cli = _client(handler)
        out = cli.download_assets(op, tmp_path, which=("splats_100k",))

        assert set(out.keys()) == {"splats_100k"}
        assert (tmp_path / "splats_100k.spz").read_bytes() == b"SPZ100K"

    def test_download_skips_missing_asset(self, tmp_path):
        """A selector whose URL is None is skipped, not downloaded."""
        world = World(
            world_id="w-nopano",
            assets=WorldAssets(
                splats=SplatAssets(spz_urls={"full_res": SPLAT_FULL_URL}),
                imagery=ImageryAssets(pano_url=None),
            ),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return _bytes(b"SPZ", request=request)

        cli = _client(handler)
        out = cli.download_assets(world, tmp_path, which=("pano", "mesh"))
        assert out == {}
        assert not (tmp_path / "pano.png").exists()

    def test_download_error_status_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return _bytes(b"gone", status=404, request=request)

        cli = _client(handler)
        with pytest.raises(WorldLabsAPIError):
            cli.download_assets(_world_with_all_assets(), tmp_path, which=("splats_full_res",))


# ── 6. Error handling ──────────────────────────────────────────────────────


class TestErrorHandling:
    def test_generate_4xx_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=422, text="bad prompt", request=request)

        cli = _client(handler)
        with pytest.raises(WorldLabsAPIError) as exc:
            cli.generate("https://example.com/x.png", confirm=True)
        assert exc.value.status_code == 422

    def test_poll_5xx_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=500, text="boom", request=request)

        cli = _client(handler)
        with pytest.raises(WorldLabsAPIError) as exc:
            cli.poll("op-err")
        assert exc.value.status_code == 500

    def test_credit_balance_returns_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/marble/v1/credits"
            assert request.headers.get("WLT-Api-Key") == "test-key"
            return _json({"balance": 12500}, request=request)

        cli = _client(handler)
        assert cli.get_credit_balance() == {"balance": 12500}

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("WORLDLABS_API_KEY", raising=False)
        cli = WorldLabsClient(api_key=None)
        with pytest.raises(MissingAPIKeyError):
            cli.poll("anything")

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("WORLDLABS_API_KEY", "env-key-123")
        cli = WorldLabsClient()
        assert cli.api_key == "env-key-123"


# ── 7. Model validation (requests forbid extras; responses tolerate them) ───


class TestModelValidation:
    # Request models stay strict: an unknown key is a bug we want to catch
    # BEFORE a credit-spending call.
    def test_generate_request_rejects_extra_key(self):
        with pytest.raises(Exception):
            GenerateRequest(
                model="marble-1.1",
                world_prompt=WorldPrompt(image_prompt=ImagePromptUri(uri="https://x/y.png")),
                bogus_field=1,  # type: ignore[call-arg]
            )

    def test_image_prompt_uri_rejects_extra_key(self):
        with pytest.raises(Exception):
            ImagePromptUri(uri="https://x/y.png", extra=1)  # type: ignore[call-arg]

    def test_image_prompt_media_asset_rejects_extra_key(self):
        with pytest.raises(Exception):
            ImagePromptMediaAsset(media_asset_id="ma", extra=1)  # type: ignore[call-arg]

    # Response models tolerate vendor-added keys: a strict model rejected the
    # payload of an ALREADY-PAID world and blocked its download (Chat 76).
    # Unknown keys are dropped, not raised.
    def test_operation_ignores_extra_key(self):
        op = Operation(operation_id="op", done=False, surprise="x")  # type: ignore[call-arg]
        assert op.operation_id == "op"
        assert not hasattr(op, "surprise")

    def test_world_assets_ignores_extra_key(self):
        wa = WorldAssets(unknown_block={})  # type: ignore[call-arg]
        assert wa.splats is None

    def test_world_ignores_extra_key(self):
        w = World(world_id="w", mystery=1)  # type: ignore[call-arg]
        assert w.world_id == "w"

    def test_live_payload_shape_validates(self):
        # The exact shape that broke download in Chat 76: cost is a billing
        # dict (not a bare int), splats carry a semantics_metadata block, and a
        # new 150k tier appears. The response envelope must parse all of it.
        op = Operation.model_validate(
            {
                "operation_id": "op-1",
                "done": True,
                "cost": {
                    "total_credits": 1580,
                    "line_items": [{"name": "World generation", "credits": 1500}],
                },
                "response": {
                    "world_id": "w-1",
                    "assets": {
                        "splats": {
                            "spz_urls": {
                                "150k": "https://x/a.spz",
                                "full_res": "https://x/f.spz",
                            },
                            "semantics_metadata": {
                                "metric_scale_factor": 2.03,
                                "ground_plane_offset": 1.21,
                            },
                        },
                        "imagery": {"pano_url": "https://x/p.png"},
                    },
                },
            }
        )
        assert op.cost["total_credits"] == 1580
        assert op.response.assets.splats.spz_urls["full_res"] == "https://x/f.spz"
        assert op.response.assets.splats.semantics_metadata["metric_scale_factor"] == 2.03

    def test_discriminated_union_selects_media_asset(self):
        wp = WorldPrompt.model_validate(
            {
                "type": "image",
                "image_prompt": {"source": "media_asset", "media_asset_id": "ma-7"},
            }
        )
        assert isinstance(wp.image_prompt, ImagePromptMediaAsset)
        assert wp.image_prompt.media_asset_id == "ma-7"

    def test_discriminated_union_selects_uri(self):
        wp = WorldPrompt.model_validate(
            {"type": "image", "image_prompt": {"source": "uri", "uri": "https://x/y.png"}}
        )
        assert isinstance(wp.image_prompt, ImagePromptUri)

    def test_operation_parses_full_world_response(self):
        """A done Operation with a full World payload validates end-to-end."""
        op = Operation.model_validate(
            {
                "operation_id": "op-full",
                "done": True,
                "response": {
                    "world_id": "w-9",
                    "display_name": "Scene",
                    "world_marble_url": "https://marble/w-9",
                    "model": "marble-1.1",
                    "assets": {
                        "splats": {"spz_urls": {"100k": SPLAT_100K_URL, "full_res": SPLAT_FULL_URL}},
                        "imagery": {"pano_url": PANO_URL},
                        "mesh": {"collider_mesh_url": MESH_URL},
                        "caption": "a room",
                        "thumbnail_url": "https://cdn/thumb.png",
                    },
                },
            }
        )
        assert op.response is not None
        assert op.response.assets is not None
        assert op.response.assets.splats is not None
        assert op.response.assets.splats.spz_urls["full_res"] == SPLAT_FULL_URL


class TestDispatcherFailedOperation:
    """A done-but-failed operation (World Labs 500, no World in the response)
    must surface as an explicit failure with retry guidance — not as a stuck
    poll or a download of an empty result (Chat 76: a paid retry hit a 500)."""

    @staticmethod
    def _patch_failed(monkeypatch):
        import json
        from maya_mcp.worldlabs import tool
        from maya_mcp.worldlabs.models import OperationError

        failed = Operation(
            operation_id="op-fail",
            done=True,
            response=None,
            error=OperationError(code=500, message="An error has happened, please retry it."),
        )

        class _FakeClient:
            api_key = "x"

            def poll(self, _op):
                return failed

        monkeypatch.setattr(tool, "_client", lambda: _FakeClient())
        return tool, json

    def test_poll_reports_failed_with_retry(self, monkeypatch):
        tool, json = self._patch_failed(monkeypatch)
        out = json.loads(tool.poll("op-fail"))
        assert out["done"] is True
        assert out["status"] == "failed"
        assert out["error"]["code"] == 500
        assert "generate" in out["next_step"].lower()
        # must NOT steer the caller to download an empty result
        assert "download (splats" not in out["next_step"].lower()

    def test_download_reports_failed_with_retry(self, monkeypatch, tmp_path):
        tool, json = self._patch_failed(monkeypatch)
        out = json.loads(tool.download("op-fail", str(tmp_path)))
        assert out["status"] == "failed"
        assert out["error"]["code"] == 500
        assert "generate" in out["next_step"].lower()
