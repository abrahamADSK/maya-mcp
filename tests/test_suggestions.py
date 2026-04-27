"""Tests for maya_mcp.suggestions (Vision3D workflow chaining hints)."""

from __future__ import annotations

import json

import pytest

from maya_mcp import suggestions as s


@pytest.fixture
def restore_rules():
    original = dict(s.SUGGESTION_RULES)
    yield s.SUGGESTION_RULES
    s.SUGGESTION_RULES.clear()
    s.SUGGESTION_RULES.update(original)


class TestHelperContract:
    def test_unknown_tool_returns_verbatim(self):
        payload = json.dumps({"status": "ok"})
        assert s.maybe_annotate_with_suggestions("not_a_tool", payload) == payload

    def test_invalid_json_returns_verbatim(self):
        assert s.maybe_annotate_with_suggestions("maya_vision3d", "oops") == "oops"

    def test_non_object_json_returns_verbatim(self):
        assert s.maybe_annotate_with_suggestions("maya_vision3d", "[1,2]") == "[1,2]"

    def test_already_annotated_is_idempotent(self, restore_rules):
        restore_rules["maya_vision3d"] = lambda _: [
            {"tool": "x", "reason": "r", "params_hint": {}}
        ]
        payload = json.dumps({
            "status": "started", "job_id": "j1",
            "next_suggested_actions": [{"tool": "keep", "reason": "r", "params_hint": {}}],
        })
        parsed = json.loads(s.maybe_annotate_with_suggestions("maya_vision3d", payload))
        assert parsed["next_suggested_actions"][0]["tool"] == "keep"

    def test_rule_raising_returns_verbatim(self, restore_rules):
        def boom(_):
            raise RuntimeError("nope")
        restore_rules["maya_vision3d"] = boom
        payload = json.dumps({"status": "ok"})
        assert s.maybe_annotate_with_suggestions("maya_vision3d", payload) == payload

    def test_suggestions_capped_at_three(self, restore_rules):
        restore_rules["maya_vision3d"] = lambda _: [
            {"tool": f"t{i}", "reason": "r", "params_hint": {}} for i in range(7)
        ]
        payload = json.dumps({"status": "ok"})
        parsed = json.loads(s.maybe_annotate_with_suggestions("maya_vision3d", payload))
        assert len(parsed["next_suggested_actions"]) == 3


class TestKillSwitch:
    def test_env_var_disables_annotation(self, monkeypatch, restore_rules):
        restore_rules["maya_vision3d"] = lambda _: [
            {"tool": "x", "reason": "r", "params_hint": {}}
        ]
        monkeypatch.setenv("MAYA_MCP_DISABLE_SUGGESTIONS", "1")
        payload = json.dumps({"status": "started", "job_id": "j1"})
        assert s.maybe_annotate_with_suggestions("maya_vision3d", payload) == payload


class TestVision3DRule:
    def test_generate_started_suggests_poll(self):
        resp = {"status": "started", "job_id": "abc12345", "output_subdir": "test"}
        out = s._suggest_after_maya_vision3d(resp)
        assert len(out) == 1
        assert out[0]["tool"] == "maya_vision3d"
        assert out[0]["params_hint"]["action"] == "poll"
        assert out[0]["params_hint"]["params"]["job_id"] == "abc12345"

    def test_poll_completed_suggests_download(self):
        resp = {
            "status": "completed",
            "elapsed_s": 45,
            "files": ["textured.glb", "mesh.glb"],
        }
        out = s._suggest_after_maya_vision3d(resp)
        assert len(out) == 1
        assert out[0]["params_hint"]["action"] == "download"

    def test_poll_completed_without_files_no_suggestion(self):
        resp = {"status": "completed", "files": []}
        assert s._suggest_after_maya_vision3d(resp) == []

    def test_poll_running_no_suggestion(self):
        # Running jobs: the next_step already prompts "poll again"; a
        # hint adding another identical poll suggestion would be noise.
        resp = {"status": "running", "elapsed_s": 10}
        assert s._suggest_after_maya_vision3d(resp) == []

    def test_download_with_textured_suggests_import(self):
        resp = {
            "status": "ok",
            "output_dir": "/Users/me/out",
            "textured": True,
            "baked_texture": False,
        }
        out = s._suggest_after_maya_vision3d(resp)
        assert len(out) == 1
        assert out[0]["tool"] == "maya_import_file"
        assert out[0]["params_hint"]["file_path"] == "/Users/me/out/textured.glb"

    def test_download_without_textured_no_suggestion(self):
        resp = {"status": "ok", "output_dir": "/tmp", "textured": False}
        assert s._suggest_after_maya_vision3d(resp) == []

    def test_error_response_no_suggestion(self):
        assert s._suggest_after_maya_vision3d({"error": "boom"}) == []

    def test_select_server_response_no_suggestion(self):
        # select_server returns {"ok": true, "url": "..."} — no status key.
        assert s._suggest_after_maya_vision3d({"ok": True, "url": "http://…"}) == []


class TestCreatePrimitiveRule:
    def test_cube_suggests_assign_material(self):
        resp = {"name": "pCube1", "type": "cube"}
        out = s._suggest_after_maya_create_primitive(resp)
        assert len(out) == 1
        assert out[0]["tool"] == "maya_assign_material"
        assert out[0]["params_hint"]["object_name"] == "pCube1"
        assert out[0]["params_hint"]["material_type"] == "aiStandardSurface"

    def test_sphere_suggests_assign_material(self):
        resp = {"name": "pSphere1", "type": "sphere"}
        out = s._suggest_after_maya_create_primitive(resp)
        assert out and out[0]["params_hint"]["object_name"] == "pSphere1"

    def test_all_primitive_types_fire(self):
        for kind in ("cube", "sphere", "cylinder", "cone", "plane", "torus"):
            resp = {"name": f"p{kind}1", "type": kind}
            assert s._suggest_after_maya_create_primitive(resp), f"{kind} did not fire"

    def test_unknown_type_no_suggestion(self):
        assert s._suggest_after_maya_create_primitive({"name": "x", "type": "mystery"}) == []

    def test_empty_name_no_suggestion(self):
        assert s._suggest_after_maya_create_primitive({"name": "", "type": "cube"}) == []

    def test_missing_name_no_suggestion(self):
        assert s._suggest_after_maya_create_primitive({"type": "cube"}) == []

    def test_error_response_no_suggestion(self):
        assert s._suggest_after_maya_create_primitive({"error": "boom"}) == []


class TestImportFileRule:
    def test_non_empty_import_suggests_save(self):
        resp = {"imported": 3, "objects": ["a", "b", "c"], "file": "/tmp/x.glb"}
        out = s._suggest_after_maya_import_file(resp)
        assert len(out) == 1
        assert out[0]["tool"] == "maya_session"
        assert out[0]["params_hint"]["action"] == "save_scene"
        assert "3 object(s)" in out[0]["reason"]

    def test_single_import_uses_singular_reason(self):
        resp = {"imported": 1, "objects": ["a"], "file": "/tmp/x.obj"}
        out = s._suggest_after_maya_import_file(resp)
        assert out and "imported object" in out[0]["reason"]

    def test_zero_import_no_suggestion(self):
        resp = {"imported": 0, "objects": [], "file": "/tmp/x.fbx"}
        assert s._suggest_after_maya_import_file(resp) == []

    def test_missing_imported_no_suggestion(self):
        assert s._suggest_after_maya_import_file({"file": "/tmp/x.fbx"}) == []

    def test_non_int_imported_no_suggestion(self):
        assert s._suggest_after_maya_import_file({"imported": "3"}) == []

    def test_error_response_no_suggestion(self):
        assert s._suggest_after_maya_import_file({"error": "import failed"}) == []


class TestCreateCameraRule:
    def test_camera_suggests_viewport_capture(self):
        resp = {"camera": "persp_shot01"}
        out = s._suggest_after_maya_create_camera(resp)
        assert len(out) == 1
        assert out[0]["tool"] == "maya_viewport_capture"
        assert out[0]["params_hint"]["camera"] == "persp_shot01"
        assert out[0]["params_hint"]["output_path"] == "/tmp/persp_shot01_preview.png"

    def test_empty_camera_no_suggestion(self):
        assert s._suggest_after_maya_create_camera({"camera": ""}) == []

    def test_missing_camera_no_suggestion(self):
        assert s._suggest_after_maya_create_camera({}) == []

    def test_error_response_no_suggestion(self):
        assert s._suggest_after_maya_create_camera({"error": "boom"}) == []


class TestCreateLightRule:
    def test_light_suggests_keyframe(self):
        resp = {"light": "directionalLight1", "type": "directional"}
        out = s._suggest_after_maya_create_light(resp)
        assert len(out) == 1
        assert out[0]["tool"] == "maya_set_keyframe"
        assert out[0]["params_hint"]["object_name"] == "directionalLight1"
        assert out[0]["params_hint"]["attribute"] == "intensity"
        assert out[0]["params_hint"]["frame"] == 1
        assert "directional light" in out[0]["reason"]

    def test_all_light_types_fire(self):
        for kind in ("directional", "point", "spot", "area", "ambient"):
            resp = {"light": f"{kind}Light1", "type": kind}
            assert s._suggest_after_maya_create_light(resp), f"{kind} did not fire"

    def test_empty_light_no_suggestion(self):
        assert s._suggest_after_maya_create_light({"light": "", "type": "point"}) == []

    def test_missing_light_no_suggestion(self):
        assert s._suggest_after_maya_create_light({"type": "point"}) == []

    def test_error_response_no_suggestion(self):
        assert s._suggest_after_maya_create_light({"error": "boom"}) == []


class TestRegistryContract:
    def test_registry_has_maya_vision3d(self):
        assert "maya_vision3d" in s.SUGGESTION_RULES

    def test_registry_has_new_rules(self):
        expected = (
            "maya_vision3d",
            "maya_create_primitive",
            "maya_import_file",
            "maya_create_camera",
            "maya_create_light",
        )
        for tool in expected:
            assert tool in s.SUGGESTION_RULES, f"{tool} missing from registry"
