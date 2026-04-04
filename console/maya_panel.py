"""Maya dockable panel — MCP Pipeline Console inside Maya.

Creates a workspaceControl that hosts the MCPChatWidget with
Maya-specific context: scene info, selection, renderer.

Usage from Maya:
    # Open the panel:
    from console.maya_panel import show
    show()

    # Register the MCP Pipeline menu in the menu bar:
    from console.maya_panel import install_menu
    install_menu()

    # Or via maya_shelf_button tool:
    maya_shelf_button(label="MCP", command="from console.maya_panel import show; show()")

The panel docks as a tab next to the Attribute Editor by default.
Position, size, and open/closed state persist across Maya sessions
via workspaceControl(retain=True).  See userSetup_snippet.py for
automatic menu registration and panel restore on Maya startup.
"""

from __future__ import annotations

import os
import maya.cmds as cmds
import maya.OpenMayaUI as omui

from pathlib import Path
from .qt_compat import QtWidgets, wrapInstance

# Panel identifiers
PANEL_NAME = "mcpPipelineConsole"
PANEL_LABEL = "MCP Pipeline Console"

# Project root — needed so uiScript/closeCommand can set up sys.path
# when Maya restores a retained workspaceControl on startup (before
# the MCP server has connected and injected the path).
_PROJECT_ROOT = str(Path(__file__).parent.parent)

# Module-level reference to keep the widget alive
_widget_instance = None

# Module-level Maya callback IDs (for cleanup)
_callback_ids = []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def show():
    """Create or show the MCP Pipeline Console panel in Maya."""
    if cmds.workspaceControl(PANEL_NAME, exists=True):
        # Update uiScript in case it was created with old code (without
        # sys.path setup).  This ensures Maya can restore the panel on
        # future restarts even before the MCP server connects.
        cmds.workspaceControl(
            PANEL_NAME, e=True,
            uiScript=_ui_script(),
            closeCommand=_close_script(),
        )
        cmds.workspaceControl(PANEL_NAME, e=True, visible=True, restore=True)
        return

    cmds.workspaceControl(
        PANEL_NAME,
        label=PANEL_LABEL,
        tabToControl=("AttributeEditor", -1),
        initialWidth=380,
        minimumWidth=300,
        widthProperty="preferred",
        retain=True,
        uiScript=_ui_script(),
        closeCommand=_close_script(),
    )


def _ui_script() -> str:
    """Python command Maya runs to build the panel contents.

    Must be self-contained: includes sys.path setup so the import
    works even on Maya restart (before MCP server connects).
    The path is baked into the string at creation time.
    """
    return (
        f"import sys; _r = r'{_PROJECT_ROOT}'; "
        f"sys.path.insert(0, _r) if _r not in sys.path else None; "
        "from console.maya_panel import _build_panel; _build_panel()"
    )


def _close_script() -> str:
    """Python command Maya runs when the panel is closed."""
    return (
        f"import sys; _r = r'{_PROJECT_ROOT}'; "
        f"sys.path.insert(0, _r) if _r not in sys.path else None; "
        "from console.maya_panel import _on_close; _on_close()"
    )


# ---------------------------------------------------------------------------
# Panel construction (called by Maya's uiScript)
# ---------------------------------------------------------------------------

def _build_panel():
    """Build the MCPChatWidget inside the workspaceControl.

    Called by Maya when the workspaceControl is created or restored.
    """
    global _widget_instance

    # Get the Qt parent from Maya's workspaceControl
    ptr = omui.MQtUtil.findControl(PANEL_NAME)
    if not ptr:
        cmds.warning(f"[MCP Panel] Cannot find control '{PANEL_NAME}'")
        return

    parent = wrapInstance(int(ptr), QtWidgets.QWidget)

    # Clean up previous instance if restoring
    if _widget_instance is not None:
        try:
            _widget_instance.setParent(None)
            _widget_instance.deleteLater()
        except RuntimeError:
            pass
        _widget_instance = None

    # Create the chat widget with Maya context function
    from .chat_widget import MCPChatWidget

    widget = MCPChatWidget(
        maya_context_fn=_get_full_maya_context,
        parent=parent,
    )

    # Add to parent layout (Maya creates a QVBoxLayout for workspaceControl)
    layout = parent.layout()
    if layout is None:
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget)

    _widget_instance = widget

    # Register Maya callbacks
    _register_callbacks(widget)

    # Set initial Maya context
    try:
        ctx = _get_full_maya_context()
        if ctx:
            widget.update_maya_context(ctx)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Maya context functions
# ---------------------------------------------------------------------------

def _get_full_maya_context() -> dict:
    """Gather complete Maya context: scene + selection + renderer."""
    ctx = {}
    ctx.update(_get_scene_context())
    ctx.update(_get_selection_context())
    return ctx


def _get_scene_context() -> dict:
    """Extract current scene info."""
    try:
        scene = cmds.file(q=True, sceneName=True) or "untitled"
        obj_count = len(cmds.ls(transforms=True))
        renderer = cmds.getAttr("defaultRenderGlobals.currentRenderer")
        return {
            "scene": scene,
            "objects": obj_count,
            "renderer": renderer or "unknown",
        }
    except Exception:
        return {}


def _get_selection_context() -> dict:
    """Extract info about the currently selected object."""
    try:
        sel = cmds.ls(selection=True, long=True)
        if not sel:
            return {"selection": None}

        obj = sel[0]
        obj_type = cmds.objectType(obj)
        info = {"selection": obj, "type": obj_type}

        # Mesh details
        shapes = cmds.listRelatives(obj, shapes=True, type="mesh") or []
        if shapes:
            try:
                info["faces"] = cmds.polyEvaluate(obj, face=True)
                info["verts"] = cmds.polyEvaluate(obj, vertex=True)
            except Exception:
                pass

        return info
    except Exception:
        return {"selection": None}


# ---------------------------------------------------------------------------
# Maya callbacks
# ---------------------------------------------------------------------------

def _register_callbacks(widget):
    """Register Maya callbacks that update the chat widget context."""
    global _callback_ids
    _unregister_callbacks()

    try:
        from maya.api import OpenMaya as om2

        # Selection changed
        sel_id = om2.MEventMessage.addEventCallback(
            "SelectionChanged",
            lambda *_: _safe_update(widget, _get_selection_context()),
        )
        _callback_ids.append(("event", sel_id))

        # Scene events
        for msg_type in (
            om2.MSceneMessage.kAfterNew,
            om2.MSceneMessage.kAfterOpen,
            om2.MSceneMessage.kAfterSave,
        ):
            cb_id = om2.MSceneMessage.addCallback(
                msg_type,
                lambda *_: _safe_update(widget, _get_scene_context()),
            )
            _callback_ids.append(("scene", cb_id))

    except Exception as exc:
        cmds.warning(f"[MCP Panel] Failed to register callbacks: {exc}")


def _unregister_callbacks():
    """Remove all registered Maya callbacks."""
    global _callback_ids
    try:
        from maya.api import OpenMaya as om2
        for cb_type, cb_id in _callback_ids:
            try:
                om2.MMessage.removeCallback(cb_id)
            except Exception:
                pass
    except ImportError:
        pass
    _callback_ids = []


def _safe_update(widget, ctx: dict):
    """Update widget context, catching errors to avoid Maya instability."""
    try:
        if widget and ctx:
            widget.update_maya_context(ctx)
    except RuntimeError:
        # Widget was deleted
        _unregister_callbacks()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _on_close():
    """Called by Maya when the workspaceControl is closed."""
    global _widget_instance
    _unregister_callbacks()
    _widget_instance = None


# ---------------------------------------------------------------------------
# Maya menu registration
# ---------------------------------------------------------------------------

_MENU_NAME = "mcpPipelineMenu"


def install_menu():
    """Create the MCP Pipeline top-level menu in Maya's main menu bar.

    Safe to call multiple times — recreates the menu if it already exists.
    Typically called from userSetup.py on Maya startup.
    """
    if cmds.menu(_MENU_NAME, exists=True):
        cmds.deleteUI(_MENU_NAME, menu=True)

    cmds.menu(
        _MENU_NAME,
        label="MCP Pipeline",
        parent="MayaWindow",
        tearOff=False,
    )
    _path_setup = (
        f"import sys; _r = r'{_PROJECT_ROOT}'; "
        f"sys.path.insert(0, _r) if _r not in sys.path else None; "
    )
    cmds.menuItem(
        label="Open Console",
        annotation="Open MCP Pipeline Console panel",
        command=_path_setup + "from console.maya_panel import show; show()",
        sourceType="python",
        parent=_MENU_NAME,
    )
    cmds.menuItem(divider=True, parent=_MENU_NAME)
    cmds.menuItem(
        label="Add Shelf Button",
        annotation="Add MCP button to the current shelf",
        command=_path_setup + "from console.maya_panel import install_shelf_button; install_shelf_button()",
        sourceType="python",
        parent=_MENU_NAME,
    )


# ---------------------------------------------------------------------------
# Shelf button helper
# ---------------------------------------------------------------------------

def install_shelf_button(shelf: str = "Custom"):
    """Add an 'MCP' button to a Maya shelf.

    Args:
        shelf: Name of the shelf tab. Defaults to "Custom".
    """
    if not cmds.shelfLayout(shelf, exists=True):
        cmds.warning(f"[MCP Panel] Shelf '{shelf}' not found")
        return

    cmds.shelfButton(
        parent=shelf,
        label="MCP",
        annotation="Open MCP Pipeline Console",
        image="pythonFamily.png",
        command="from console.maya_panel import show; show()",
        sourceType="python",
    )
    print(f"[MCP Panel] Shelf button added to '{shelf}'")
