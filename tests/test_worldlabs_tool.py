"""
test_worldlabs_tool.py
======================
Unit tests for the non-Maya action logic in
``src/maya_mcp/worldlabs/tool.py``.

ALL network I/O and subprocess calls are mocked — no real API calls, no
real gsbox, no credits spent.  The approach mirrors ``test_worldlabs_client.py``
(monkeypatch on the module-level ``_client`` factory) so tool.py can be tested
without touching server.py or any other non-test file.

Coverage
--------
- health: no key → error payload; with key → ok + credits
- generate: confirm=False → confirmation_required with model + approx_credits;
            confirm=True → started + operation_id;
            FileNotFoundError (missing image) → error payload
- poll: running → next_step "poll again";
        done with assets → world_id + available splats/pano/mesh;
        done with error field → error in result
- download: not_ready → status=not_ready; done → status=ok + paths
- convert: success → ok + ply_path;
           SpzConversionUnavailable → error + hint;
           SpzConversionError → error (no hint)
- build_maya_code: returns str containing ``build_environment(`` and the ply
  path; repr-safe (no raw quotes or backslashes from arbitrary paths)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


import maya_mcp.worldlabs.tool as tool_mod
from maya_mcp.worldlabs.client import (
    GenerationNotConfirmedError,
    WorldLabsError,
)
from maya_mcp.worldlabs.convert import SpzConversionError, SpzConversionUnavailable
from maya_mcp.worldlabs.models import (
    ImageryAssets,
    MeshAssets,
    Operation,
    OperationError,
    SplatAssets,
    World,
    WorldAssets,
)

# ── Helpers ────────────────────────────────────────────────────────────────

SPLAT_FULL_URL = "https://cdn.example.com/world/full_res.spz"
SPLAT_100K_URL = "https://cdn.example.com/world/100k.spz"
PANO_URL = "https://cdn.example.com/world/pano.png"
MESH_URL = "https://cdn.example.com/world/collider.glb"


def _world_with_all_assets() -> World:
    return World(
        world_id="w-test",
        assets=WorldAssets(
            splats=SplatAssets(
                spz_urls={"full_res": SPLAT_FULL_URL, "100k": SPLAT_100K_URL}
            ),
            imagery=ImageryAssets(pano_url=PANO_URL),
            mesh=MeshAssets(collider_mesh_url=MESH_URL),
        ),
    )


def _mock_client(**attrs):
    """Build a MagicMock WorldLabsClient with sensible defaults."""
    client = MagicMock()
    client.api_key = "test-key"
    client.base_url = "https://api.worldlabs.ai"
    client.get_credit_balance.return_value = {"balance": 5000}
    for k, v in attrs.items():
        setattr(client, k, v)
    return client


# ── 1. health() ────────────────────────────────────────────────────────────


class TestHealth:
    def test_no_api_key_returns_error(self, monkeypatch):
        """No WORLDLABS_API_KEY → error payload, never attempts a network call."""
        client = _mock_client(api_key="")
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.health())

        assert "error" in result
        assert "WORLDLABS_API_KEY" in result["error"]
        client.get_credit_balance.assert_not_called()

    def test_with_api_key_returns_ok(self, monkeypatch):
        """Valid API key → status=ok with credit balance."""
        client = _mock_client()
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.health())

        assert result["status"] == "ok"
        assert result["api_key"] == "present"
        assert result["credits"] == {"balance": 5000}

    def test_credit_balance_failure_is_non_fatal(self, monkeypatch):
        """If get_credit_balance raises, health still returns ok (best-effort)."""
        client = _mock_client()
        client.get_credit_balance.side_effect = RuntimeError("network timeout")
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.health())

        assert result["status"] == "ok"
        assert "warning" in result["credits"]


# ── 2. generate() ──────────────────────────────────────────────────────────


class TestGenerate:
    def test_confirm_false_returns_confirmation_required(self, monkeypatch):
        """No confirm → confirmation_required payload, no network call."""
        client = _mock_client()
        client.generate.side_effect = GenerationNotConfirmedError(
            model="marble-1.1", approx_credits=1500
        )
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.generate("https://example.com/x.png", "out/"))

        assert result["status"] == "confirmation_required"
        assert result["model"] == "marble-1.1"
        assert result["approx_credits"] == 1500
        assert "next_step" in result

    def test_confirm_true_returns_started(self, monkeypatch):
        """confirm=True → client.generate is called and status=started is returned."""
        client = _mock_client()
        client.generate.return_value = "op-abc123"
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(
            tool_mod.generate(
                "https://example.com/x.png",
                "out/subdir",
                model="marble-1.1",
                confirm=True,
            )
        )

        assert result["status"] == "started"
        assert result["operation_id"] == "op-abc123"
        assert result["output_subdir"] == "out/subdir"
        assert "next_step" in result
        client.generate.assert_called_once()

    def test_confirm_true_passes_model_and_prompt(self, monkeypatch):
        """model and text_prompt are forwarded to client.generate."""
        client = _mock_client()
        client.generate.return_value = "op-xyz"
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        tool_mod.generate(
            "https://example.com/img.png",
            "out/",
            model="marble-1.1-plus",
            text_prompt="sunny meadow",
            confirm=True,
        )

        call_kwargs = client.generate.call_args
        assert call_kwargs.kwargs.get("model") == "marble-1.1-plus" or \
               "marble-1.1-plus" in call_kwargs.args
        assert call_kwargs.kwargs.get("text_prompt") == "sunny meadow" or \
               "sunny meadow" in call_kwargs.args

    def test_missing_image_file_returns_error(self, monkeypatch):
        """FileNotFoundError for a missing local image → error payload."""
        client = _mock_client()
        client.generate.side_effect = FileNotFoundError("/no/such/image.png")
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(
            tool_mod.generate("/no/such/image.png", "out/", confirm=True)
        )

        assert "error" in result
        assert "image not found" in result["error"]

    def test_worldlabs_api_error_returns_error(self, monkeypatch):
        """A WorldLabsError from client.generate → error payload (no raise)."""
        client = _mock_client()
        client.generate.side_effect = WorldLabsError("server returned 500")
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(
            tool_mod.generate("https://example.com/x.png", "out/", confirm=True)
        )

        assert "error" in result


# ── 3. poll() ──────────────────────────────────────────────────────────────


class TestPoll:
    def test_running_operation(self, monkeypatch):
        """Running operation → done=False, next_step suggests polling again."""
        client = _mock_client()
        client.poll.return_value = Operation(operation_id="op-1", done=False)
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.poll("op-1"))

        assert result["operation_id"] == "op-1"
        assert result["done"] is False
        assert "poll again" in result["next_step"].lower()

    def test_done_with_assets(self, monkeypatch):
        """Done operation with a full World → world_id + available buckets."""
        client = _mock_client()
        client.poll.return_value = Operation(
            operation_id="op-2",
            done=True,
            response=_world_with_all_assets(),
        )
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.poll("op-2"))

        assert result["done"] is True
        assert result["world_id"] == "w-test"
        assert "full_res" in result["available"]["splats_spz"]
        assert result["available"]["pano"] is True
        assert result["available"]["mesh"] is True
        assert "next_step" in result

    def test_done_with_error_field(self, monkeypatch):
        """Operation that completed with an error → error dict in result."""
        client = _mock_client()
        client.poll.return_value = Operation(
            operation_id="op-3",
            done=True,
            error=OperationError(code=500, message="generation failed"),
        )
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.poll("op-3"))

        assert result["done"] is True
        assert "error" in result
        assert result["error"]["code"] == 500
        assert result["error"]["message"] == "generation failed"

    def test_worldlabs_error_returns_error_payload(self, monkeypatch):
        """WorldLabsError from client.poll → error payload (no raise)."""
        client = _mock_client()
        client.poll.side_effect = WorldLabsError("poll 404")
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.poll("op-missing"))

        assert "error" in result


# ── 4. download() ──────────────────────────────────────────────────────────


class TestDownload:
    def test_not_ready_returns_not_ready_status(self, monkeypatch, tmp_path):
        """Operation still running → status=not_ready, no files downloaded."""
        client = _mock_client()
        client.poll.return_value = Operation(operation_id="op-4", done=False)
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.download("op-4", str(tmp_path)))

        assert result["status"] == "not_ready"
        assert result["operation_id"] == "op-4"
        client.download_assets.assert_not_called()

    def test_done_returns_ok_with_paths(self, monkeypatch, tmp_path):
        """Done operation → status=ok with downloaded file paths."""
        client = _mock_client()
        client.poll.return_value = Operation(
            operation_id="op-5",
            done=True,
            response=_world_with_all_assets(),
        )
        spz_path = tmp_path / "splats_full_res.spz"
        pano_path = tmp_path / "pano.png"
        client.download_assets.return_value = {
            "splats_full_res": spz_path,
            "pano": pano_path,
        }
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.download("op-5", str(tmp_path)))

        assert result["status"] == "ok"
        assert "splats_full_res" in result["downloaded"]
        assert str(spz_path) in result["downloaded"]["splats_full_res"]
        assert "next_step" in result

    def test_next_step_mentions_spz_path(self, monkeypatch, tmp_path):
        """next_step should reference the SPZ path so the caller can convert."""
        client = _mock_client()
        client.poll.return_value = Operation(
            operation_id="op-6",
            done=True,
            response=_world_with_all_assets(),
        )
        spz_path = tmp_path / "splats_full_res.spz"
        client.download_assets.return_value = {"splats_full_res": spz_path}
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.download("op-6", str(tmp_path)))

        assert str(spz_path) in result["next_step"] or "spz_path" in result["next_step"]

    def test_worldlabs_error_returns_error_payload(self, monkeypatch, tmp_path):
        """WorldLabsError during download → error payload (no raise)."""
        client = _mock_client()
        client.poll.side_effect = WorldLabsError("download 403")
        monkeypatch.setattr(tool_mod, "_client", lambda: client)

        result = json.loads(tool_mod.download("op-7", str(tmp_path)))

        assert "error" in result


# ── 5. convert() ───────────────────────────────────────────────────────────


class TestConvert:
    def test_success_returns_ok_and_ply_path(self, monkeypatch, tmp_path):
        """Successful conversion → status=ok and ply_path in result."""
        ply = tmp_path / "world.ply"

        monkeypatch.setattr(tool_mod, "convert_spz_to_ply", lambda spz, ply_path: ply)

        result = json.loads(
            tool_mod.convert(str(tmp_path / "world.spz"), str(ply))
        )

        assert result["status"] == "ok"
        assert result["ply_path"] == str(ply)
        assert "next_step" in result

    def test_spz_conversion_unavailable_returns_error_with_hint(
        self, monkeypatch, tmp_path
    ):
        """SpzConversionUnavailable → error + hint about installing gsbox."""

        def _raise(spz, ply_path):
            raise SpzConversionUnavailable("no gsbox on PATH")

        monkeypatch.setattr(tool_mod, "convert_spz_to_ply", _raise)

        result = json.loads(tool_mod.convert(str(tmp_path / "world.spz")))

        assert "error" in result
        assert "hint" in result

    def test_spz_conversion_error_returns_error_no_hint(
        self, monkeypatch, tmp_path
    ):
        """SpzConversionError (converter ran but failed) → error, no hint."""

        def _raise(spz, ply_path):
            raise SpzConversionError("converter exited with rc=2")

        monkeypatch.setattr(tool_mod, "convert_spz_to_ply", _raise)

        result = json.loads(tool_mod.convert(str(tmp_path / "world.spz")))

        assert "error" in result
        assert "hint" not in result

    def test_missing_spz_file_returns_error(self, monkeypatch, tmp_path):
        """FileNotFoundError from the converter seam → error payload."""

        def _raise(spz, ply_path):
            raise FileNotFoundError("/no/such/world.spz")

        monkeypatch.setattr(tool_mod, "convert_spz_to_ply", _raise)

        result = json.loads(tool_mod.convert("/no/such/world.spz"))

        assert "error" in result
        assert "SPZ not found" in result["error"]

    def test_default_ply_path_is_none(self, monkeypatch, tmp_path):
        """When ply_path is omitted, convert_spz_to_ply is called with None."""
        called_with: dict = {}

        def _fake(spz, ply_path):
            called_with["ply_path"] = ply_path
            return Path(str(spz).replace(".spz", ".ply"))

        monkeypatch.setattr(tool_mod, "convert_spz_to_ply", _fake)

        spz = tmp_path / "world.spz"
        tool_mod.convert(str(spz))

        assert called_with["ply_path"] is None


# ── 6. build_maya_code() ───────────────────────────────────────────────────


class TestBuildMayaCode:
    def test_returns_string(self):
        code = tool_mod.build_maya_code("/tmp/scene.ply")
        assert isinstance(code, str)
        assert len(code) > 0

    def test_contains_build_environment_call(self):
        """The returned code must include the build_environment( call so Maya runs it."""
        code = tool_mod.build_maya_code("/tmp/scene.ply")
        assert "build_environment(" in code

    def test_ply_path_present_in_code(self):
        """The PLY path must appear in the assembled code so Maya receives it."""
        ply = "/projects/worlds/my_scene.ply"
        code = tool_mod.build_maya_code(ply)
        assert ply in code

    def test_ply_path_repr_safe(self):
        """Paths with spaces/backslashes must be repr'd safely (no raw injection)."""
        ply = r"C:\Users\artist\worlds\my scene.ply"
        code = tool_mod.build_maya_code(ply)
        # repr() wraps in quotes and escapes backslashes — the raw path string
        # should NOT appear verbatim (that would break the Python code string)
        assert ply not in code or repr(ply)[1:-1] in code  # repr body is safe
        # The code must still be syntactically reasonable
        assert "build_environment(" in code

    def test_pano_path_forwarded(self):
        """pano_path kwarg must appear in the generated call."""
        code = tool_mod.build_maya_code("/tmp/scene.ply", pano_path="/tmp/pano.png")
        assert "/tmp/pano.png" in code

    def test_pano_path_none_by_default(self):
        """When pano_path is omitted, None must be forwarded explicitly."""
        code = tool_mod.build_maya_code("/tmp/scene.ply")
        assert "pano_path=None" in code

    def test_eye_height_forwarded(self):
        """eye_height is coerced to float and included in the call."""
        code = tool_mod.build_maya_code("/tmp/scene.ply", eye_height=2.0)
        assert "eye_height=2.0" in code

    def test_proxy_step_forwarded(self):
        """proxy_step is coerced to int and included in the call."""
        code = tool_mod.build_maya_code("/tmp/scene.ply", proxy_step=4)
        assert "proxy_step=4" in code

    def test_relight_false_by_default(self):
        """relight defaults to False."""
        code = tool_mod.build_maya_code("/tmp/scene.ply")
        assert "relight=False" in code

    def test_relight_true_forwarded(self):
        """relight=True is coerced to bool and forwarded."""
        code = tool_mod.build_maya_code("/tmp/scene.ply", relight=True)
        assert "relight=True" in code

    def test_code_includes_json_import(self):
        """The generated snippet must import json (it wraps result in json.dumps)."""
        code = tool_mod.build_maya_code("/tmp/scene.ply")
        assert "import json" in code

    def test_maya_build_source_prepended(self):
        """The full maya_build.py source must be in the code (not just the call)."""
        code = tool_mod.build_maya_code("/tmp/scene.ply")
        # maya_build.py defines build_environment — that name must be defined,
        # not just called, in the returned string.
        assert "def build_environment" in code
