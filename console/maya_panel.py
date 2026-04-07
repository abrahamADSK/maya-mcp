"""Maya dockable panel — MCP Pipeline Console inside Maya.

Provides a workspaceControl-based panel that hosts the MCPChatWidget
with live Maya context (scene, selection, renderer).

Path resolution:
    src/maya_mcp/server.py injects a guarded block into Maya's userSetup.py on
    first connect, which adds the project root to sys.path.  This ensures
    all imports work on Maya restart — even before the MCP server connects
    — so the retained workspaceControl can restore its uiScript reliably.

Public API:
    show()           — Create or restore the panel
    install_menu()   — Add "MCP Pipeline" to Maya's menu bar
"""

from __future__ import annotations

import maya.cmds as cmds
import maya.OpenMayaUI as omui

from .qt_compat import QtWidgets, wrapInstance

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PANEL_NAME = "mcpPipelineConsole"
PANEL_LABEL = "MCP Pipeline Console"
_MENU_NAME = "mcpPipelineMenu"

# ---------------------------------------------------------------------------
# Widget + callback state
# ---------------------------------------------------------------------------

_widget_ref = None
_cb_ids: list = []

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def show():
    """Create or restore the MCP Pipeline Console panel."""
    if cmds.workspaceControl(PANEL_NAME, exists=True):
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
        uiScript="from console.maya_panel import _build_panel; _build_panel()",
        closeCommand="from console.maya_panel import _on_close; _on_close()",
    )


def install_menu():
    """Create the MCP Pipeline top-level menu in Maya's menu bar.

    Idempotent — safe to call on every startup.
    """
    if cmds.menu(_MENU_NAME, exists=True):
        cmds.deleteUI(_MENU_NAME, menu=True)

    cmds.menu(
        _MENU_NAME,
        label="MCP Pipeline",
        parent="MayaWindow",
        tearOff=False,
    )
    cmds.menuItem(
        label="Open Console",
        annotation="Open MCP Pipeline Console panel",
        command="from console.maya_panel import show; show()",
        sourceType="python",
        parent=_MENU_NAME,
    )
    cmds.menuItem(divider=True, parent=_MENU_NAME)
    cmds.menuItem(
        label="Add Shelf Button",
        annotation="Add MCP button to the current shelf",
        command="from console.maya_panel import install_shelf_button; install_shelf_button()",
        sourceType="python",
        parent=_MENU_NAME,
    )


# ---------------------------------------------------------------------------
# Panel build (called by Maya's uiScript)
# ---------------------------------------------------------------------------

def _build_panel():
    """Build the MCPChatWidget inside the workspaceControl.

    Maya calls this via uiScript when the control is created or restored.
    """
    global _widget_ref

    ptr = omui.MQtUtil.findControl(PANEL_NAME)
    if not ptr:
        cmds.warning(f"[MCP] Cannot find control '{PANEL_NAME}'")
        return

    parent = wrapInstance(int(ptr), QtWidgets.QWidget)

    # Tear down previous instance (e.g. on session restore)
    if _widget_ref is not None:
        try:
            _widget_ref.setParent(None)
            _widget_ref.deleteLater()
        except RuntimeError:
            pass
        _widget_ref = None

    from .chat_widget import MCPChatWidget

    widget = MCPChatWidget(
        maya_context_fn=_maya_context,
        parent=parent,
    )

    layout = parent.layout()
    if layout is None:
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget)

    _widget_ref = widget
    _register_callbacks(widget)

    try:
        ctx = _maya_context()
        if ctx:
            widget.update_maya_context(ctx)
    except Exception:
        pass


def _on_close():
    """Called by Maya when the panel is closed."""
    global _widget_ref
    _unregister_callbacks()
    _widget_ref = None


# ---------------------------------------------------------------------------
# Maya context
# ---------------------------------------------------------------------------

def _maya_context() -> dict:
    """Gather scene + selection context for the chat widget."""
    ctx = {}
    ctx.update(_scene_ctx())
    ctx.update(_selection_ctx())
    return ctx


def _scene_ctx() -> dict:
    try:
        return {
            "scene": cmds.file(q=True, sceneName=True) or "untitled",
            "objects": len(cmds.ls(transforms=True)),
            "renderer": cmds.getAttr("defaultRenderGlobals.currentRenderer") or "unknown",
        }
    except Exception:
        return {}


def _selection_ctx() -> dict:
    try:
        sel = cmds.ls(selection=True, long=True)
        if not sel:
            return {"selection": None}

        obj = sel[0]
        info = {"selection": obj, "type": cmds.objectType(obj)}

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
    global _cb_ids
    _unregister_callbacks()
    try:
        from maya.api import OpenMaya as om2

        sid = om2.MEventMessage.addEventCallback(
            "SelectionChanged",
            lambda *_: _safe_update(widget, _selection_ctx()),
        )
        _cb_ids.append(sid)

        for msg in (om2.MSceneMessage.kAfterNew,
                     om2.MSceneMessage.kAfterOpen,
                     om2.MSceneMessage.kAfterSave):
            cid = om2.MSceneMessage.addCallback(
                msg, lambda *_: _safe_update(widget, _scene_ctx()))
            _cb_ids.append(cid)
    except Exception as exc:
        cmds.warning(f"[MCP] Callback registration failed: {exc}")


def _unregister_callbacks():
    global _cb_ids
    try:
        from maya.api import OpenMaya as om2
        for cid in _cb_ids:
            try:
                om2.MMessage.removeCallback(cid)
            except Exception:
                pass
    except ImportError:
        pass
    _cb_ids = []


def _safe_update(widget, ctx: dict):
    try:
        if widget and ctx:
            widget.update_maya_context(ctx)
    except RuntimeError:
        _unregister_callbacks()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shelf button
# ---------------------------------------------------------------------------

def install_shelf_button(shelf: str = "Custom"):
    """Add an 'MCP' button to a Maya shelf."""
    if not cmds.shelfLayout(shelf, exists=True):
        cmds.warning(f"[MCP] Shelf '{shelf}' not found")
        return
    cmds.shelfButton(
        parent=shelf,
        label="MCP",
        annotation="Open MCP Pipeline Console",
        image="pythonFamily.png",
        command="from console.maya_panel import show; show()",
        sourceType="python",
    )
    print(f"[MCP] Shelf button added to '{shelf}'")
