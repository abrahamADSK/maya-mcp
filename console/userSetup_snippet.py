"""userSetup.py snippet — paste this into your Maya userSetup.py.

This registers the MCP Pipeline menu on every Maya startup and ensures
the console panel auto-restores if it was left open in the previous session.

Location of userSetup.py (create it if it doesn't exist):
  - Windows: %USERPROFILE%/Documents/maya/<version>/scripts/userSetup.py
  - macOS:   ~/Library/Preferences/Autodesk/maya/<version>/scripts/userSetup.py
  - Linux:   ~/maya/<version>/scripts/userSetup.py

If you already have a userSetup.py, just append the block below.
"""

# ── MCP Pipeline Console — auto-setup ──────────────────────────────────
# Deferred execution ensures Maya's UI is fully loaded before we touch menus.
import maya.cmds as cmds
import maya.utils


def _mcp_deferred_setup():
    """Register MCP menu + restore panel if it was open last session."""
    try:
        from console.maya_panel import install_menu
        install_menu()
        print("[MCP Pipeline] Menu registered.")
    except Exception as exc:
        cmds.warning(f"[MCP Pipeline] Could not register menu: {exc}")

    # workspaceControl with retain=True handles auto-restore automatically:
    # Maya remembers the panel was open and calls uiScript to rebuild it.
    # No extra code needed here — just make sure `console` is on PYTHONPATH.


maya.utils.executeDeferred(_mcp_deferred_setup)
# ── end MCP Pipeline Console ───────────────────────────────────────────
