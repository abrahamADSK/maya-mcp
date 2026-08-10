"""Which Maya does `launch` actually open? (Chat 94)

``open -a Maya`` hands the choice to LaunchServices; with 2026 and 2027 both
installed it opens an arbitrary one. Everything downstream is version-specific
— the Command Port, the panel bootstrap, api_graph.json, the publish templates
— so a launcher that cannot name the version it started is a silent trap, the
same one as launching Flame by app name via AppleScript.

The contract mirrors the rest of the ecosystem's handling of ambiguous state
(Vision3D URL, console project context, the native Flame link): exactly one
candidate → use it, several → choice_required, never a guess.
"""

from __future__ import annotations

import pytest

from maya_mcp import server

M2026 = "/Applications/Autodesk/maya2026/Maya.app"
M2027 = "/Applications/Autodesk/maya2027/Maya.app"


@pytest.fixture
def fake_installs(monkeypatch):
    """Pretend a given set of bundles is installed, with MAYA_APP unset."""
    def _apply(candidates, maya_app=""):
        monkeypatch.setattr(server, "_discover_maya_apps", lambda: sorted(candidates))
        monkeypatch.setattr(server, "MAYA_APP", maya_app)
        monkeypatch.setattr(server.os.path, "exists", lambda p: p in candidates)
    return _apply


def test_single_install_is_used_without_asking(fake_installs):
    fake_installs([M2027])
    bundle, candidates, error = server._resolve_maya_app()
    assert error is None
    assert bundle == M2027
    assert candidates == [M2027]


def test_two_installs_refuse_to_guess(fake_installs):
    """The actual bug: two Mayas and no selector must NOT silently pick one."""
    fake_installs([M2026, M2027])
    bundle, candidates, error = server._resolve_maya_app()
    assert bundle is None
    assert error is not None
    assert "choice_required" in error["error"]
    assert set(error["candidates"]) == {M2026, M2027}
    # the hint must be actionable — a literal MAYA_APP line to paste
    assert "MAYA_APP=" in error["hint"]


def test_no_install_is_an_actionable_error(fake_installs):
    fake_installs([])
    bundle, _candidates, error = server._resolve_maya_app()
    assert bundle is None
    assert "No Maya installation" in error["error"]
    assert "MAYA_APP" in error["hint"]


def test_absolute_maya_app_wins_over_discovery(fake_installs):
    fake_installs([M2026, M2027], maya_app=M2026)
    bundle, _candidates, error = server._resolve_maya_app()
    assert error is None
    assert bundle == M2026


def test_absolute_maya_app_that_does_not_exist_errors(fake_installs):
    fake_installs([M2026, M2027], maya_app="/Applications/Autodesk/maya2019/Maya.app")
    bundle, _candidates, error = server._resolve_maya_app()
    assert bundle is None
    assert "does not exist" in error["error"]


def test_version_selector_picks_exactly_one(fake_installs):
    fake_installs([M2026, M2027], maya_app="2027")
    bundle, _candidates, error = server._resolve_maya_app()
    assert error is None
    assert bundle == M2027


def test_ambiguous_selector_is_refused(fake_installs):
    """"maya" matches both — that is exactly the ambiguity we are removing."""
    fake_installs([M2026, M2027], maya_app="maya")
    bundle, _candidates, error = server._resolve_maya_app()
    assert bundle is None
    assert "exactly one" in error["error"]


def test_launch_opens_a_bundle_path_never_a_bare_app_name(monkeypatch):
    """Regression: the argv passed to `open` must be a resolved .app path."""
    import asyncio
    import json

    captured = {}

    async def _fake_run(cmd, timeout=60):
        captured["argv"] = cmd
        return 1, "", "boom"          # fail fast; we only care about argv

    def _boom_ping():
        raise RuntimeError("not running")

    monkeypatch.setattr(server, "_discover_maya_apps", lambda: [M2026, M2027])
    monkeypatch.setattr(server, "MAYA_APP", M2027)
    monkeypatch.setattr(server.os.path, "exists", lambda p: True)
    monkeypatch.setattr(server, "_run_cmd", _fake_run)
    monkeypatch.setattr(server.bridge, "ping", _boom_ping)

    json.loads(asyncio.run(server._do_launch({})))
    assert captured["argv"] == ["open", "-a", M2027]
    assert "Maya" != captured["argv"][2], "must never be the bare app name"


def test_launch_surfaces_the_choice_instead_of_opening_something(monkeypatch):
    """With two installs and no selector, launch must not run `open` at all."""
    import asyncio
    import json

    called = {"ran": False}

    async def _fake_run(cmd, timeout=60):
        called["ran"] = True
        return 0, "", ""

    def _boom_ping():
        raise RuntimeError("not running")

    monkeypatch.setattr(server, "_discover_maya_apps", lambda: [M2026, M2027])
    monkeypatch.setattr(server, "MAYA_APP", "")
    monkeypatch.setattr(server, "_run_cmd", _fake_run)
    monkeypatch.setattr(server.bridge, "ping", _boom_ping)

    data = json.loads(asyncio.run(server._do_launch({})))
    assert called["ran"] is False, "must not launch anything while ambiguous"
    assert "choice_required" in data["error"]
