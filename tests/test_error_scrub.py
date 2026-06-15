"""test_error_scrub.py — unit tests for the shared OPSEC scrub+truncate helper.

``error_scrub`` is byte-identical across fpt-mcp / maya-mcp / flame-mcp
(canonical at ~/Projects/error_scrub_canonical.py). These tests pin the two
guarantees every server relies on at its error boundary: credential redaction
and length bounding. maya-mcp applies it in ``server._handle_error``.
"""

from maya_mcp.error_scrub import MAX_ERROR_CHARS, safe_error_message, scrub_secrets


class TestScrubSecrets:
    def test_redacts_key_value(self):
        assert scrub_secrets("boom api_key=ABC123 end") == "boom api_key=***redacted*** end"

    def test_redacts_colon_form(self):
        assert "***redacted***" in scrub_secrets("password: hunter2")

    def test_longer_key_matched_whole(self):
        assert scrub_secrets("script_key=XYZ") == "script_key=***redacted***"

    def test_naming_a_field_without_value_is_left_intact(self):
        msg = "invalid script_name or api_key"
        assert scrub_secrets(msg) == msg


class TestSafeErrorMessage:
    def test_scrub_then_truncate(self):
        out = safe_error_message(RuntimeError("token=SEKRET " + "x" * 1000))
        assert len(out) == MAX_ERROR_CHARS == 300
        assert "***redacted***" in out

    def test_empty_message_falls_back_to_class_name(self):
        assert safe_error_message(ValueError("")) == "ValueError"

    def test_custom_max_len(self):
        assert safe_error_message(RuntimeError("y" * 50), max_len=10) == "y" * 10
