"""Which Maya does `launch` actually open? (Chat 94)

`open -a Maya` hands the choice to LaunchServices; with 2026 and 2027 both
installed it opens an arbitrary one and the tool never learns which. Everything
downstream is version-specific — the Command Port, the panel bootstrap,
api_graph.json, the publish templates — so the launcher must resolve, and
report, a concrete bundle. Same trap as launching Flame by app name via
AppleScript.

Version authority (Chat 65/68 user rule): for pipeline work the version that
opens is the one ShotGrid Desktop marks as DEFAULT, which fpt-mcp's resolve_app
reads off the SG Software entity. maya-mcp has no ShotGrid access, so `launch`
mirrors that resolver's FALLBACK layer — newest install, deterministic, with a
warning naming the others and pointing at fpt_launch_app. It never blocks.
"""

from __future__ import annotations

import pytest

from maya_mcp import server

M2024 = "/Applications/Autodesk/maya2024/Maya.app"
M2026 = "/Applications/Autodesk/maya2026/Maya.app"
M2027 = "/Applications/Autodesk/maya2027/Maya.app"


@pytest.fixture
def fake_installs(monkeypatch):
    """Pretend a given set of bundles is installed, with MAYA_APP unset."""
    def _apply(paths, maya_app=""):
        monkeypatch.setattr(server, "MAYA_APP", maya_app)
        monkeypatch.setattr(server.os.path, "exists", lambda p: p in paths)
        import glob as _glob
        monkeypatch.setattr(_glob, "glob", lambda _pattern: list(paths))
    return _apply


def test_discovery_sorts_newest_first(fake_installs):
    """Mirrors fpt-mcp's _os_scan_maya: parse the year, newest first."""
    fake_installs([M2024, M2027, M2026])
    assert server._discover_maya_apps() == [M2027, M2026, M2024]


def test_single_install_is_used_with_no_warning(fake_installs):
    fake_installs([M2027])
    bundle, warnings, error = server._resolve_maya_app()
    assert error is None
    assert bundle == M2027
    assert warnings == []


def test_several_installs_open_the_newest_and_say_so(fake_installs):
    """The agreed model: never LaunchServices roulette, never a blocking prompt."""
    fake_installs([M2026, M2027])
    bundle, warnings, error = server._resolve_maya_app()
    assert error is None
    assert bundle == M2027, "must open the newest install"
    assert len(warnings) == 1
    # the warning must name the road not taken AND the real authority
    assert M2026 in warnings[0]
    assert "ShotGrid Desktop" in warnings[0]
    assert "fpt_launch_app" in warnings[0]


def test_no_install_is_an_actionable_error(fake_installs):
    fake_installs([])
    bundle, _warnings, error = server._resolve_maya_app()
    assert bundle is None
    assert "No Maya installation" in error["error"]


def test_optional_absolute_pin_wins_over_newest(fake_installs):
    """MAYA_APP is an optional pin for a box that must not follow "newest"."""
    fake_installs([M2026, M2027], maya_app=M2026)
    bundle, _warnings, error = server._resolve_maya_app()
    assert error is None
    assert bundle == M2026


def test_pin_that_does_not_exist_errors(fake_installs):
    fake_installs([M2026, M2027], maya_app="/Applications/Autodesk/maya2019/Maya.app")
    bundle, _warnings, error = server._resolve_maya_app()
    assert bundle is None
    assert "does not exist" in error["error"]


def test_version_selector_picks_exactly_one(fake_installs):
    fake_installs([M2026, M2027], maya_app="2027")
    bundle, _warnings, error = server._resolve_maya_app()
    assert error is None
    assert bundle == M2027


def test_ambiguous_selector_is_refused(fake_installs):
    """"maya" matches both — a pin that does not pin is a bug, not a default."""
    fake_installs([M2026, M2027], maya_app="maya")
    bundle, _warnings, error = server._resolve_maya_app()
    assert bundle is None
    assert "exactly one" in error["error"]


def test_launch_opens_a_bundle_path_never_a_bare_app_name(monkeypatch):
    """Regression for the actual bug: the argv passed to `open` is a .app path."""
    import asyncio
    import json

    captured = {}

    async def _fake_run(cmd, timeout=60):
        captured["argv"] = cmd
        return 1, "", "boom"          # fail fast; we only care about argv

    def _boom_ping():
        raise RuntimeError("not running")

    monkeypatch.setattr(server, "_discover_maya_apps", lambda: [M2027, M2026])
    monkeypatch.setattr(server, "MAYA_APP", "")
    monkeypatch.setattr(server, "_run_cmd", _fake_run)
    monkeypatch.setattr(server.bridge, "ping", _boom_ping)

    json.loads(asyncio.run(server._do_launch({})))
    assert captured["argv"] == ["open", "-a", M2027]
    assert captured["argv"][2] != "Maya", "must never be the bare app name"
