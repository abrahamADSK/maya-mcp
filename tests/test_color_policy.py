"""Tests for the review/preview colour-management policy (``color_policy.py``).

``color_policy`` emits **code strings** that run inside Maya (over the Command
Port), so these tests do two things a string-match alone cannot:

1. ``compile()`` every emitted block — catches f-string brace / indentation
   bugs in the generated source without a live Maya (the same class of bug that
   shipped a broken playblast flag in v1.18.0).
2. ``exec()`` the blocks against a fake ``maya.cmds`` and assert the *behaviour*:
   the view transform is pinned and restored; the Arnold output transform is
   enabled for ``preview`` and force-DISABLED for ``exr`` (the EXR guardrail).

A final sync guard pins ``review_build.py`` (which ships standalone over the
wire and cannot import this module inside Maya) to the same flag names + default
view, so the two copies cannot silently drift (memory
``feedback_mock_paths_blindspot`` / atomic-docs discipline).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from maya_mcp import color_policy


# ── fake maya.cmds ───────────────────────────────────────────────────────────

class _FakeCmds:
    """Minimal ``maya.cmds`` stand-in for colour-management + Arnold driver.

    The Arnold driver's colour is steered via a ``colorManagement`` enum
    (``Raw:Use View Transform:Use Output Transform`` on live Maya 2027, Chat 79),
    so the fake models ``attributeQuery(exists/listEnum)`` + ``setAttr`` on it.
    """

    def __init__(self, views, current="sRGB gamma (sRGB)",
                 cm_enum="Raw:Use View Transform:Use Output Transform",
                 cm_attr_exists=True, driver_exists=True):
        self.views = list(views)
        self.current_view = current
        self.cm_enum = cm_enum
        self.cm_attr_exists = cm_attr_exists
        self.driver_exists = driver_exists
        self.set_values: dict = {}
        self.output_enabled = None

    def colorManagementPrefs(self, *a, **k):
        if k.get("query"):
            if k.get("viewTransformNames"):
                return list(self.views)
            if k.get("viewTransformName"):
                return self.current_view
            if k.get("outputTransformNames"):
                return list(self.views)
            return None
        if k.get("edit"):
            if "viewTransformName" in k:
                self.current_view = k["viewTransformName"]
            if "outputTransformEnabled" in k:
                self.output_enabled = k["outputTransformEnabled"]
        return None

    def objExists(self, _name):
        return self.driver_exists

    def attributeQuery(self, attr, **k):
        if attr != "colorManagement":
            return False if k.get("exists") else []
        if k.get("exists"):
            return self.cm_attr_exists
        if k.get("listEnum"):
            return [self.cm_enum]
        return None

    def setAttr(self, attr, value):
        self.set_values[attr] = value


def _run(code: str, cmds: _FakeCmds) -> dict:
    """exec ``code`` with ``import maya.cmds`` resolving to ``cmds``; return the ns."""
    maya_mod = types.ModuleType("maya")
    maya_mod.cmds = cmds
    saved = {k: sys.modules.get(k) for k in ("maya", "maya.cmds")}
    sys.modules["maya"] = maya_mod
    sys.modules["maya.cmds"] = cmds
    try:
        ns: dict = {}
        exec(compile(code, "<color_policy>", "exec"), ns)
        return ns
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ── view transform (A1: VP2 playblasts) ──────────────────────────────────────

def test_emitted_blocks_compile():
    """Every emitted block is syntactically valid Python (no f-string/indent bug)."""
    for code in (
        color_policy.view_transform_apply_code(),
        color_policy.view_transform_restore_code(),
        color_policy.arnold_output_transform_code("preview"),
        color_policy.arnold_output_transform_code("exr"),
    ):
        compile(code, "<gen>", "exec")  # raises SyntaxError if malformed


def test_apply_pins_view_when_available_and_captures_previous():
    cmds = _FakeCmds(views=["Raw (sRGB)", color_policy.DEFAULT_REVIEW_VIEW],
                     current="Raw (sRGB)")
    ns = _run(color_policy.view_transform_apply_code(), cmds)
    # captured the previous view, then switched to the configured one
    assert ns["_mcp_cp_prev_view"] == "Raw (sRGB)"
    assert cmds.current_view == color_policy.DEFAULT_REVIEW_VIEW


def test_apply_is_noop_when_view_absent():
    """Unknown view (e.g. an ACES-only config) → no change, prev stays None."""
    cmds = _FakeCmds(views=["sRGB (ACES)", "ACES 1.0 SDR-video"], current="sRGB (ACES)")
    ns = _run(color_policy.view_transform_apply_code(), cmds)
    assert ns["_mcp_cp_prev_view"] is None
    assert cmds.current_view == "sRGB (ACES)"  # untouched


def test_restore_puts_back_the_previous_view():
    cmds = _FakeCmds(views=[color_policy.DEFAULT_REVIEW_VIEW], current="X")
    # apply then restore round-trips the view back to its original value
    apply_ns = _run(color_policy.view_transform_apply_code(), cmds)
    assert cmds.current_view == color_policy.DEFAULT_REVIEW_VIEW
    restore = color_policy.view_transform_restore_code()
    maya_mod = types.ModuleType("maya")
    maya_mod.cmds = cmds
    sys.modules["maya"] = maya_mod
    sys.modules["maya.cmds"] = cmds
    try:
        exec(compile(restore, "<r>", "exec"), {"_mcp_cp_prev_view": apply_ns["_mcp_cp_prev_view"]})
    finally:
        sys.modules.pop("maya", None)
        sys.modules.pop("maya.cmds", None)
    assert cmds.current_view == "X"


def test_injection_skeleton_compiles():
    """The viewport_capture injection pattern (apply + try/finally + indented
    restore) must compile — guards the manual 4-space indent in server.py."""
    apply_code = color_policy.view_transform_apply_code()
    restore = color_policy.view_transform_restore_code()
    restore_indented = "\n".join(
        ("    " + ln) if ln.strip() else ln
        for ln in restore.strip("\n").splitlines()
    )
    skeleton = f"""
import maya.cmds as cmds
{apply_code}
try:
    pass
finally:
    pass
{restore_indented}
"""
    compile(skeleton, "<skeleton>", "exec")


# ── Arnold output transform (A2: file renders, EXR guardrail) ────────────────

def test_arnold_preview_sets_use_output_transform():
    """preview mode: driver enum → "Use Output Transform" (by name) + global on.

    Enum index 2 in ``Raw:Use View Transform:Use Output Transform`` — mapped by
    name, so the value is the discovered index, not a hardcoded number.
    """
    cmds = _FakeCmds(views=[color_policy.DEFAULT_REVIEW_VIEW])
    ns = _run(color_policy.arnold_output_transform_code("preview"), cmds)
    assert cmds.set_values["defaultArnoldDriver.colorManagement"] == 2
    assert cmds.output_enabled is True                 # global output transform on
    assert ns["result"]["driver_color_management"] == "Use Output Transform"


def test_arnold_exr_forces_driver_raw():
    """EXR guardrail: driver enum → "Raw" (index 0) so a display transform is
    never baked into a scene-linear EXR; the global is left untouched."""
    cmds = _FakeCmds(views=[color_policy.DEFAULT_REVIEW_VIEW])
    ns = _run(color_policy.arnold_output_transform_code("exr"), cmds)
    assert cmds.set_values["defaultArnoldDriver.colorManagement"] == 0
    assert cmds.output_enabled is None                 # global NOT touched for EXR
    assert ns["result"]["driver_color_management"] == "Raw"


def test_arnold_skips_when_enum_lacks_target():
    """An mtoa whose enum has no "Use Output Transform" → no setAttr, clean None."""
    cmds = _FakeCmds(views=[color_policy.DEFAULT_REVIEW_VIEW], cm_enum="Raw:sRGB")
    ns = _run(color_policy.arnold_output_transform_code("preview"), cmds)
    assert "defaultArnoldDriver.colorManagement" not in cmds.set_values
    assert ns["result"]["driver_color_management"] is None


def test_arnold_handles_missing_attr_and_driver():
    """No colorManagement attr, or no driver at all → no crash, no driver change."""
    no_attr = _FakeCmds(views=[color_policy.DEFAULT_REVIEW_VIEW], cm_attr_exists=False)
    ns1 = _run(color_policy.arnold_output_transform_code("preview"), no_attr)
    assert ns1["result"]["driver_color_management"] is None
    assert "defaultArnoldDriver.colorManagement" not in no_attr.set_values

    no_driver = _FakeCmds(views=[color_policy.DEFAULT_REVIEW_VIEW], driver_exists=False)
    ns2 = _run(color_policy.arnold_output_transform_code("preview"), no_driver)
    assert ns2["result"]["driver_color_management"] is None


# ── sync guard: review_build.py must not drift from color_policy ─────────────

def test_review_build_matches_color_policy_flags():
    src = (Path(color_policy.__file__).parent / "review_build.py").read_text()
    # same default review view literal (review_build hardcodes it — see its docstring)
    assert color_policy.DEFAULT_REVIEW_VIEW in src
    # same colour-management flags, so the standalone copy cannot diverge
    assert "cmEnabled" in src
    assert "viewTransformName" in src
    assert "viewTransformNames" in src
    # the recipe accepts the view_transform parameter the server passes
    assert "view_transform" in src
