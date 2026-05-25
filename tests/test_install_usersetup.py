"""
test_install_usersetup.py
=========================
Unit tests for the install.sh userSetup.py block management logic
(``build_block``, ``upsert_block``, ``_parse_block_version``).

The Python functions are extracted inline from the install.sh heredoc by
compiling the same source the installer itself runs.  This avoids duplicating
the logic and guarantees the tests exercise the exact code that ships.

Scenarios covered:
  (a) Fresh install  — file absent → block created, contains port line and
                        version marker.
  (b) Re-run same ver — existing up-to-date block → "unchanged" (true no-op).
  (c) Stale block     — old block missing version marker → refreshed to current
                        version, port line present.
  (d) Version bump    — block at v(BLOCK_VERSION-1) → regenerated on re-run.
  (e) Port line kwarg — generated block always uses ``name=":PORT"`` form, never
                        positional (Maya 2027 regression guard).
  (f) Content only change — same version but different repo root → regenerated.

Run with:
    pytest tests/test_install_usersetup.py -v
"""

from __future__ import annotations

import re
import textwrap
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Extract the installer's Python module from install.sh at import time.
# The Step 7 heredoc is delimited by:
#   "${VENV_PYTHON}" - "${REPO_ROOT}" "${USERSETUP_PORT}" <<'PYEOF'
#   ...
#   PYEOF
# We read install.sh, locate that heredoc, and exec its content into a fresh
# module so the tests can import build_block / upsert_block / BLOCK_VERSION
# etc. directly.
# ---------------------------------------------------------------------------

_INSTALL_SH = Path(__file__).parent.parent / "install.sh"

_STEP7_DELIMITER_RE = re.compile(
    r"""\"\$\{VENV_PYTHON\}\" - \"\$\{REPO_ROOT\}\" \"\$\{USERSETUP_PORT\}\" <<'PYEOF'""",
    re.MULTILINE,
)


def _extract_step7_source(install_sh: Path) -> str:
    """Extract the Python source of the Step 7 heredoc from install.sh."""
    text = install_sh.read_text(encoding="utf-8")
    match = _STEP7_DELIMITER_RE.search(text)
    if not match:
        raise RuntimeError(
            "Could not locate Step 7 heredoc in install.sh. "
            "Check that the delimiter line is still "
            '"${{VENV_PYTHON}}" - "${{REPO_ROOT}}" "${{USERSETUP_PORT}}" <<\'PYEOF\''
        )
    start = match.end()
    end_marker = "\nPYEOF\n"
    end = text.find(end_marker, start)
    if end == -1:
        raise RuntimeError("Could not locate closing PYEOF in install.sh Step 7 heredoc.")
    return text[start:end]


def _make_step7_module() -> types.ModuleType:
    """Compile and execute the Step 7 heredoc, returning it as a module."""
    src = _extract_step7_source(_INSTALL_SH)
    # Provide minimal sys.argv so the top-level assignment doesn't blow up.
    # sys.exit(main()) at the bottom calls sys.exit(0) — we suppress it.
    import sys as _sys

    module = types.ModuleType("_install_step7")
    # Patch sys.argv for the module
    saved_argv = _sys.argv[:]
    _sys.argv = ["install.sh", "/fake/repo/root", "8100"]

    old_exit = _sys.exit

    def _no_exit(code=0):
        pass  # suppress the sys.exit(main()) call

    _sys.exit = _no_exit
    try:
        exec(compile(src, "<install.sh:step7>", "exec"), module.__dict__)  # noqa: S102
    finally:
        _sys.argv = saved_argv
        _sys.exit = old_exit

    return module


# Build the module once at import time so each test function can use it.
_step7 = _make_step7_module()

SENTINEL_START: str = _step7.SENTINEL_START
SENTINEL_END: str = _step7.SENTINEL_END
BLOCK_VERSION: int = _step7.BLOCK_VERSION
build_block = _step7.build_block
upsert_block = _step7.upsert_block
_parse_block_version = _step7._parse_block_version


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_FAKE_ROOT = "/home/user/maya-mcp"
_FAKE_PORT = "8100"


def _make_stale_block(version: int | None = None) -> str:
    """Return a minimal stale block between sentinels.

    ``version=None`` → no version marker at all (pre-versioning install).
    ``version=N``    → a marker at that version.
    """
    ver_line = ""
    if version is not None:
        ver_line = f"\n# MCP Pipeline Console block v{version}"
    return (
        f"{SENTINEL_START}{ver_line}\n"
        "import sys as _mcp_sys\n"
        "# Old block — no command-port line\n"
        f"{SENTINEL_END}\n"
    )


# ---------------------------------------------------------------------------
# (a) Fresh install
# ---------------------------------------------------------------------------

class TestFreshInstall:
    """Block is absent; install creates it."""

    def test_creates_file_when_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        result = upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        assert result == "created", f"Expected 'created', got {result!r}"
        assert target.is_file()

    def test_created_block_contains_sentinel_start(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        assert SENTINEL_START in target.read_text()

    def test_created_block_contains_sentinel_end(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        assert SENTINEL_END in target.read_text()

    def test_created_block_contains_port_name_kwarg(self, tmp_path: Path) -> None:
        """Port opening must use name= kwarg form (Maya 2027 regression guard)."""
        target = tmp_path / "userSetup.py"
        upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        content = target.read_text()
        assert f'commandPort(name=":{_FAKE_PORT}"' in content, (
            "Generated block must open Command Port via name= kwarg "
            "(positional form silently fails on Maya 2027)"
        )

    def test_created_block_contains_version_marker(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        content = target.read_text()
        assert f"# MCP Pipeline Console block v{BLOCK_VERSION}" in content

    def test_preserves_prior_content(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        prior = "# existing userSetup content\nimport maya.cmds as cmds\n"
        target.write_text(prior, encoding="utf-8")
        upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        content = target.read_text()
        assert "# existing userSetup content" in content
        assert SENTINEL_START in content


# ---------------------------------------------------------------------------
# (b) Re-run with same version → no-op
# ---------------------------------------------------------------------------

class TestIdempotentRerun:
    """Running install twice at the same version must be a no-op."""

    def test_second_run_is_unchanged(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        block = build_block(_FAKE_ROOT, _FAKE_PORT)
        upsert_block(target, block)
        result2 = upsert_block(target, block)
        assert result2 == "unchanged", (
            f"Second run should be 'unchanged', got {result2!r}"
        )

    def test_second_run_does_not_mutate_file(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        block = build_block(_FAKE_ROOT, _FAKE_PORT)
        upsert_block(target, block)
        content_before = target.read_text()
        upsert_block(target, block)
        content_after = target.read_text()
        assert content_before == content_after

    def test_no_duplicate_sentinels_after_rerun(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        block = build_block(_FAKE_ROOT, _FAKE_PORT)
        upsert_block(target, block)
        upsert_block(target, block)
        content = target.read_text()
        assert content.count(SENTINEL_START) == 1
        assert content.count(SENTINEL_END) == 1


# ---------------------------------------------------------------------------
# (c) Stale block — missing version marker
# ---------------------------------------------------------------------------

class TestStaleBlockNoVersionMarker:
    """A pre-versioning block (no version line) must be regenerated."""

    def test_stale_block_returns_updated(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        target.write_text(_make_stale_block(version=None), encoding="utf-8")
        result = upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        assert result == "updated", (
            f"Stale (no version marker) block should return 'updated', got {result!r}"
        )

    def test_stale_block_gets_port_line(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        target.write_text(_make_stale_block(version=None), encoding="utf-8")
        upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        content = target.read_text()
        assert f'commandPort(name=":{_FAKE_PORT}"' in content, (
            "Refreshed block must contain the command-port opening line"
        )

    def test_stale_block_gets_current_version_marker(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        target.write_text(_make_stale_block(version=None), encoding="utf-8")
        upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        content = target.read_text()
        assert f"# MCP Pipeline Console block v{BLOCK_VERSION}" in content

    def test_stale_block_old_content_removed(self, tmp_path: Path) -> None:
        """The 'Old block' comment from the stale block must be gone."""
        target = tmp_path / "userSetup.py"
        target.write_text(_make_stale_block(version=None), encoding="utf-8")
        upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        content = target.read_text()
        assert "# Old block" not in content

    def test_parse_block_version_no_marker_returns_zero(self) -> None:
        stale = _make_stale_block(version=None)
        assert _parse_block_version(stale) == 0


# ---------------------------------------------------------------------------
# (d) Version bump triggers regeneration
# ---------------------------------------------------------------------------

class TestVersionBumpRegeneration:
    """A block at version N < BLOCK_VERSION must be regenerated."""

    def test_lower_version_returns_updated(self, tmp_path: Path) -> None:
        old_ver = BLOCK_VERSION - 1
        if old_ver < 0:
            pytest.skip("BLOCK_VERSION is 0 — no lower version to test")
        target = tmp_path / "userSetup.py"
        target.write_text(_make_stale_block(version=old_ver), encoding="utf-8")
        result = upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        assert result == "updated", (
            f"Block at v{old_ver} should be regenerated when BLOCK_VERSION={BLOCK_VERSION}"
        )

    def test_lower_version_upgraded_to_current(self, tmp_path: Path) -> None:
        old_ver = BLOCK_VERSION - 1
        if old_ver < 0:
            pytest.skip("BLOCK_VERSION is 0 — no lower version to test")
        target = tmp_path / "userSetup.py"
        target.write_text(_make_stale_block(version=old_ver), encoding="utf-8")
        upsert_block(target, build_block(_FAKE_ROOT, _FAKE_PORT))
        content = target.read_text()
        assert _parse_block_version(content) == BLOCK_VERSION

    def test_same_version_not_regenerated_unnecessarily(self, tmp_path: Path) -> None:
        """Exact current version + identical content must not churn the file."""
        target = tmp_path / "userSetup.py"
        block = build_block(_FAKE_ROOT, _FAKE_PORT)
        upsert_block(target, block)
        result = upsert_block(target, block)
        assert result == "unchanged"

    def test_parse_block_version_reads_correctly(self) -> None:
        block = build_block(_FAKE_ROOT, _FAKE_PORT)
        assert _parse_block_version(block) == BLOCK_VERSION


# ---------------------------------------------------------------------------
# (e) Command-port kwarg form (Maya 2027 regression guard)
# ---------------------------------------------------------------------------

class TestCommandPortKwargForm:
    """The generated block must always use name= kwarg, never positional."""

    def test_no_positional_commandport_open_in_generated_block(self) -> None:
        """The OPEN (non-query) form must use name= kwarg, not positional.

        The bug (Maya 2027): ``cmds.commandPort(":8100", sourceType="mel")``
        silently ignores the positional name when sourceType= is also given.
        The correct form is ``cmds.commandPort(name=":8100", sourceType="mel")``.

        The QUERY form ``commandPort(":8100", query=True)`` uses the positional
        form intentionally (query mode is not affected by the Maya 2027 bug) —
        this test excludes that line from the check.
        """
        block = build_block(_FAKE_ROOT, _FAKE_PORT)
        port_str = f":{_FAKE_PORT}"
        # Detect the positional OPEN form: commandPort("<port>", sourceType=...
        # This must NOT appear; the correct form is commandPort(name="<port>", ...
        positional_open_pattern = re.compile(
            r'commandPort\s*\(\s*"' + re.escape(port_str) + r'"\s*,\s*sourceType\s*=',
        )
        assert not positional_open_pattern.search(block), (
            "commandPort OPEN must use name= kwarg form; "
            "positional form silently fails on Maya 2027 when sourceType= is also given"
        )

    def test_kwarg_commandport_open_present(self) -> None:
        block = build_block(_FAKE_ROOT, _FAKE_PORT)
        assert f'commandPort(name=":{_FAKE_PORT}", sourceType="mel")' in block

    def test_kwarg_commandport_query_present(self) -> None:
        """Query form (probe before opening) must also be present."""
        block = build_block(_FAKE_ROOT, _FAKE_PORT)
        assert f'commandPort(":{_FAKE_PORT}", query=True)' in block


# ---------------------------------------------------------------------------
# (f) Content-only change (different repo root → regenerate)
# ---------------------------------------------------------------------------

class TestContentChangeRegeneration:
    """Block at current version but different repo root must be updated."""

    def test_repo_root_change_triggers_update(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        upsert_block(target, build_block("/old/repo/path", _FAKE_PORT))
        result = upsert_block(target, build_block("/new/repo/path", _FAKE_PORT))
        assert result == "updated"

    def test_updated_block_has_new_root(self, tmp_path: Path) -> None:
        target = tmp_path / "userSetup.py"
        upsert_block(target, build_block("/old/repo/path", _FAKE_PORT))
        upsert_block(target, build_block("/new/repo/path", _FAKE_PORT))
        content = target.read_text()
        assert "/new/repo/path" in content
        assert "/old/repo/path" not in content
