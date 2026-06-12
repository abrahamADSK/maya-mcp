"""
test_trust_gate.py
==================
Tests for the ``_model_can_write()`` corpus-poisoning trust gate in
``src/maya_mcp/server.py``.

``learn_pattern`` appends model-authored patterns to the RAG source docs. To
stop a weaker / local model from poisoning the corpus, writes are gated behind
``_model_can_write()``: only the top cloud tiers (Opus, Fable) may write by
default; every other model (Sonnet, Qwen, GLM, "unknown") is read-only and its
candidate is merely staged for review. An operator can override the allowlist
via ``config.json -> write_allowed_models`` (which, when present, *replaces*
the built-in set rather than extending it).

These tests pin that contract. The gate reads the model name and the config
through the module-level ``_get_current_model`` / ``_get_config`` helpers, so
each case monkeypatches those two functions — no real ``config.json`` on disk
and no Maya / MCP SDK / network access required (the conftest MCP stub makes
``import maya_mcp.server`` work).
"""

import pytest

from maya_mcp import server as srv


# ── Helper ───────────────────────────────────────────────────────────────

def _patch_gate(monkeypatch, model: str, config: dict) -> None:
    """Point the trust gate at a fixed model name and config dict."""
    monkeypatch.setattr(srv, "_get_current_model", lambda: model)
    monkeypatch.setattr(srv, "_get_config", lambda: config)


# ── Default allowlist (no config override) ───────────────────────────────

@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-8",
        "claude-opus-4-1-20250805",
        "claude-fable-1",
        "Claude-Opus-4-8",  # case-insensitive: the gate lower()s the model
    ],
)
def test_trusted_models_can_write(monkeypatch, model):
    _patch_gate(monkeypatch, model, {})
    assert srv._model_can_write() is True


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-5",      # Sonnet is explicitly read-only
        "claude-haiku-4",
        "qwen2.5-coder:32b",      # local model
        "glm-4-9b",               # local model
        "unknown",                # _get_current_model default when no config
        "gpt-4o",
    ],
)
def test_untrusted_models_cannot_write(monkeypatch, model):
    _patch_gate(monkeypatch, model, {})
    assert srv._model_can_write() is False


def test_default_allowlist_membership_is_locked():
    """The built-in write allowlist must stay exactly {Opus, Fable}: a silent
    addition (or accidental widening to Sonnet/local) would open the corpus to
    poisoning. Lock it so any change is a deliberate, reviewed edit."""
    assert srv.WRITE_ALLOWED_MODELS == {"claude-opus", "claude-fable"}


# ── config.json override (write_allowed_models) ──────────────────────────

def test_config_override_allows_custom_model(monkeypatch):
    """A non-default model named in config.json -> write_allowed_models is
    granted write access (substring, case-insensitive match)."""
    _patch_gate(monkeypatch, "qwen2.5-coder:32b", {"write_allowed_models": ["qwen"]})
    assert srv._model_can_write() is True


def test_config_override_replaces_default_set(monkeypatch):
    """When write_allowed_models is present it REPLACES the built-in set: a
    model trusted by default (Opus) is denied if it is not in the override
    list. This makes the override an explicit, exclusive allowlist."""
    _patch_gate(monkeypatch, "claude-opus-4-8", {"write_allowed_models": ["qwen"]})
    assert srv._model_can_write() is False


def test_empty_config_override_falls_back_to_default(monkeypatch):
    """An empty / falsy write_allowed_models list does NOT lock everyone out —
    it falls back to the built-in Opus/Fable allowlist."""
    _patch_gate(monkeypatch, "claude-opus-4-8", {"write_allowed_models": []})
    assert srv._model_can_write() is True
