"""Reusable MCP chat widget — embeddable QWidget for any host.

This widget contains the full chat UI (header, chat area, input bar,
server status dots) and can be embedded:
  - Inside Maya via workspaceControl  (maya_panel.py)
  - Inside a QMainWindow              (standalone — chat_window.py)
  - Inside any other Qt container

It has NO dependency on QMainWindow, QApplication, or Maya.
"""

from __future__ import annotations

import html
import re
from typing import Optional

from .qt_compat import QtWidgets, QtCore, QtGui

from .claude_worker import AVAILABLE_MODELS, ClaudeWorker
from .server_panel import ServerStatusBar, detect_mcp_servers


# ---------------------------------------------------------------------------
# Stylesheet — Maya-blue dark theme  (applied to the widget subtree)
# ---------------------------------------------------------------------------

DARK_STYLE = """
QWidget#mcpChatRoot {
    background-color: #1a1a2e;
}
QLabel#title {
    color: #00b4d8;
    font-size: 14px;
    font-weight: 700;
}
QLabel#contextBadge {
    background-color: #0f3460;
    color: #94a3b8;
    padding: 2px 8px;
    border-radius: 8px;
    font-size: 11px;
}
QLabel#contextBadge[active="true"] {
    background-color: #164e63;
    color: #67e8f9;
}
QTextBrowser#chat {
    background-color: #1a1a2e;
    color: #cbd5e1;
    border: none;
    font-size: 13px;
    selection-background-color: #334155;
}
QLineEdit#input {
    background-color: #1e293b;
    border: 1px solid #334155;
    color: #e0e0e0;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 13px;
}
QLineEdit#input:focus {
    border-color: #00b4d8;
}
QPushButton#sendBtn {
    background-color: #00b4d8;
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#sendBtn:hover {
    background-color: #0096b7;
}
QPushButton#sendBtn:disabled {
    background-color: #334155;
}
QPushButton#quickBtn {
    background-color: #1e293b;
    color: #94a3b8;
    border: 1px solid #334155;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton#quickBtn:hover {
    background-color: #334155;
    color: #e0e0e0;
}
QWidget#header {
    background-color: #16213e;
}
QWidget#inputBar {
    background-color: #16213e;
}
QLabel#statusDot {
    min-width: 8px; max-width: 8px;
    min-height: 8px; max-height: 8px;
    border-radius: 4px;
}
"""


# ---------------------------------------------------------------------------
# Minimal markdown → HTML converter
# ---------------------------------------------------------------------------

def _md_to_html(text: str) -> str:
    """Convert simple markdown to HTML for QTextBrowser."""
    lines = text.split("\n")
    out: list[str] = []
    in_code = False

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                out.append(
                    '<pre style="background:#0f172a;color:#93c5fd;'
                    'padding:10px;border-radius:6px;font-size:12px;'
                    'overflow-x:auto;">'
                )
                in_code = True
            continue

        if in_code:
            out.append(html.escape(line))
            continue

        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            sizes = {1: "16px", 2: "14px", 3: "13px"}
            out.append(
                f'<p style="font-size:{sizes[level]};font-weight:700;'
                f'color:#e0e0e0;margin:6px 0 3px;">{html.escape(m.group(2))}</p>'
            )
            continue

        if re.match(r"^\s*[-*]\s+", line):
            content = re.sub(r"^\s*[-*]\s+", "", line)
            content = _inline_fmt(content)
            out.append(f'<p style="margin:2px 0 2px 14px;">&#8226; {content}</p>')
            continue

        if line.strip():
            out.append(f"<p>{_inline_fmt(line)}</p>")
        else:
            out.append("<br>")

    if in_code:
        out.append("</pre>")

    return "\n".join(out)


def _inline_fmt(text: str) -> str:
    """Apply inline markdown formatting."""
    text = html.escape(text)
    text = re.sub(
        r"`([^`]+)`",
        r'<code style="background:#1e293b;padding:1px 4px;'
        r'border-radius:3px;color:#93c5fd;">\1</code>',
        text,
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a style="color:#67e8f9;" href="\2">\1</a>',
        text,
    )
    return text


# ---------------------------------------------------------------------------
# Quick Action definitions
# ---------------------------------------------------------------------------

_QUICK_ACTIONS = [
    ("Gen3D", "Generate a 3D model using Vision3D of: "),
    ("Publish", "Publish the current Maya scene to ShotGrid"),
    ("Docs", "Search the Maya API documentation for: "),
]


# ---------------------------------------------------------------------------
# MCPChatWidget — the reusable core
# ---------------------------------------------------------------------------

class MCPChatWidget(QtWidgets.QWidget):
    """Self-contained MCP chat widget.

    Hosts: chat display, input bar, quick actions, server status dots.
    Connects to Claude Code CLI via ClaudeWorker (QThread).

    Parameters
    ----------
    maya_context_fn : callable, optional
        If provided, called before each prompt to get a dict of
        Maya-specific context (scene, selection, renderer, etc.).
        Only set this when running inside Maya.
    parent : QWidget, optional
    """

    def __init__(
        self,
        maya_context_fn=None,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName("mcpChatRoot")
        self._history: list[dict] = []
        self._context: dict = {}
        self._maya_context_fn = maya_context_fn
        self._worker: Optional[ClaudeWorker] = None
        self._progress_lines: list[str] = []
        self._servers: dict = {}
        self._selected_model_idx = 0

        self._build_ui()
        self.setStyleSheet(DARK_STYLE)

        # Discover MCP servers and start health checks
        self._servers = detect_mcp_servers()
        self._status_bar.update_servers(self._servers)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header ──
        header = QtWidgets.QWidget()
        header.setObjectName("header")
        h_lay = QtWidgets.QHBoxLayout(header)
        h_lay.setContentsMargins(10, 6, 10, 6)
        h_lay.setSpacing(8)

        title = QtWidgets.QLabel("MCP Console")
        title.setObjectName("title")
        h_lay.addWidget(title)

        self._context_badge = QtWidgets.QLabel("No context")
        self._context_badge.setObjectName("contextBadge")
        self._context_badge.setProperty("active", False)
        h_lay.addWidget(self._context_badge)

        h_lay.addStretch()

        # Model selector combo
        self._model_combo = QtWidgets.QComboBox()
        for label, _, _ in AVAILABLE_MODELS:
            self._model_combo.addItem(label)
        self._model_combo.setCurrentIndex(self._selected_model_idx)
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        self._model_combo.setStyleSheet(
            "QComboBox { background: #1e293b; color: #e0e0e0; border: 1px solid #334155; "
            "border-radius: 6px; padding: 2px 6px; font-size: 11px; }"
        )
        h_lay.addWidget(self._model_combo)

        # Compact server status dots
        self._status_bar = ServerStatusBar()
        h_lay.addWidget(self._status_bar)

        layout.addWidget(header)

        # ── Chat area ──
        self._chat = QtWidgets.QTextBrowser()
        self._chat.setObjectName("chat")
        self._chat.setOpenExternalLinks(True)
        self._chat.setReadOnly(True)
        layout.addWidget(self._chat, 1)

        # ── Quick actions ──
        qa_bar = QtWidgets.QWidget()
        qa_lay = QtWidgets.QHBoxLayout(qa_bar)
        qa_lay.setContentsMargins(10, 4, 10, 0)
        qa_lay.setSpacing(6)

        for label, prompt_template in _QUICK_ACTIONS:
            btn = QtWidgets.QPushButton(label)
            btn.setObjectName("quickBtn")
            btn.clicked.connect(lambda checked=False, t=prompt_template: self._quick_action(t))
            qa_lay.addWidget(btn)

        qa_lay.addStretch()
        layout.addWidget(qa_bar)

        # ── Input bar ──
        input_bar = QtWidgets.QWidget()
        input_bar.setObjectName("inputBar")
        i_lay = QtWidgets.QHBoxLayout(input_bar)
        i_lay.setContentsMargins(10, 6, 10, 8)
        i_lay.setSpacing(8)

        self._input = QtWidgets.QLineEdit()
        self._input.setObjectName("input")
        self._input.setPlaceholderText("Ask anything — Maya, ShotGrid, Vision3D, Flame...")
        self._input.returnPressed.connect(self._send)
        i_lay.addWidget(self._input, 1)

        self._send_btn = QtWidgets.QPushButton("Send")
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.clicked.connect(self._send)
        i_lay.addWidget(self._send_btn)

        layout.addWidget(input_bar)

        self._input.setFocus()

    # ------------------------------------------------------------------
    # Model selection
    # ------------------------------------------------------------------

    def _on_model_changed(self, index: int):
        """Called when the user picks a different model in the combo."""
        self._selected_model_idx = index

    def _get_selected_model(self) -> tuple[str, str]:
        """Return (model_id, backend) for the currently selected model."""
        _, model_id, backend = AVAILABLE_MODELS[self._selected_model_idx]
        return model_id, backend

    # ------------------------------------------------------------------
    # Quick actions
    # ------------------------------------------------------------------

    def _quick_action(self, prompt_template: str):
        """Pre-fill the input with a prompt template."""
        self._input.setText(prompt_template)
        self._input.setFocus()
        # Place cursor at end so user can append
        self._input.setCursorPosition(len(prompt_template))

    # ------------------------------------------------------------------
    # Context updates (from Maya callbacks or external code)
    # ------------------------------------------------------------------

    def update_context(self, ctx: dict):
        """Merge new pipeline/Maya context."""
        self._context.update(ctx)
        self._refresh_badge()

    def update_maya_context(self, ctx: dict):
        """Update Maya-specific context (selection, scene)."""
        self._context.update(ctx)
        self._refresh_badge()

    def _refresh_badge(self):
        """Update the context badge text based on current context."""
        parts = []

        # Scene info
        scene = self._context.get("scene")
        if scene and scene != "untitled":
            import os
            parts.append(os.path.basename(scene))

        # Object count
        obj_count = self._context.get("objects")
        if obj_count:
            parts.append(f"{obj_count} obj")

        # Selection
        sel = self._context.get("selection")
        if sel:
            sel_short = sel.rsplit("|", 1)[-1] if "|" in sel else sel
            sel_type = self._context.get("type", "")
            faces = self._context.get("faces")
            if faces:
                parts.append(f"{sel_short} ({sel_type}, {faces}f)")
            else:
                parts.append(sel_short)

        # ShotGrid entity
        etype = self._context.get("entity_type")
        eid = self._context.get("entity_id")
        if etype and eid:
            parts.append(f"{etype} #{eid}")

        if parts:
            self._context_badge.setText(" · ".join(parts))
            self._context_badge.setProperty("active", True)
        else:
            self._context_badge.setText("No context")
            self._context_badge.setProperty("active", False)

        self._context_badge.style().unpolish(self._context_badge)
        self._context_badge.style().polish(self._context_badge)

    # ------------------------------------------------------------------
    # Chat logic
    # ------------------------------------------------------------------

    def _append_bubble(self, html_content: str, role: str):
        colors = {
            "user":      ("text-align:right;", "#0f3460", "#e0e0e0"),
            "assistant": ("text-align:left;",  "#1e293b", "#cbd5e1"),
            "error":     ("text-align:left;",  "#7f1d1d", "#fca5a5"),
            "thinking":  ("text-align:left;",  "#1e293b", "#64748b"),
        }
        align, bg, fg = colors.get(role, colors["assistant"])
        bubble = (
            f'<div style="{align}margin:4px 3px;">'
            f'<div style="display:inline-block;background:{bg};color:{fg};'
            f'padding:8px 12px;border-radius:10px;max-width:90%;'
            f'text-align:left;font-size:13px;line-height:1.5;">'
            f'{html_content}'
            f'</div></div>'
        )
        self._chat.append(bubble)

    def _update_last_bubble(self, html_content: str, role: str):
        colors = {
            "user":      ("text-align:right;", "#0f3460", "#e0e0e0"),
            "assistant": ("text-align:left;",  "#1e293b", "#cbd5e1"),
            "error":     ("text-align:left;",  "#7f1d1d", "#fca5a5"),
            "thinking":  ("text-align:left;",  "#1e293b", "#64748b"),
        }
        align, bg, fg = colors.get(role, colors["assistant"])
        bubble = (
            f'<div style="{align}margin:4px 3px;">'
            f'<div style="display:inline-block;background:{bg};color:{fg};'
            f'padding:8px 12px;border-radius:10px;max-width:90%;'
            f'text-align:left;font-size:13px;line-height:1.5;">'
            f'{html_content}'
            f'</div></div>'
        )
        cursor = self._chat.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.movePosition(
            cursor.MoveOperation.StartOfBlock, cursor.MoveMode.KeepAnchor
        )
        cursor.removeSelectedText()
        cursor.deletePreviousChar()
        self._chat.setTextCursor(cursor)
        self._chat.append(bubble)

    def _send(self):
        text = self._input.text().strip()
        if not text:
            return

        self._input.clear()
        self._send_btn.setEnabled(False)
        self._append_bubble(html.escape(text), "user")
        self._history.append({"role": "user", "text": text})

        # Gather fresh Maya context if available
        if self._maya_context_fn:
            try:
                maya_ctx = self._maya_context_fn()
                if maya_ctx:
                    self._context.update(maya_ctx)
                    self._refresh_badge()
            except Exception:
                pass

        self._progress_lines = []
        self._append_bubble("<i>Thinking...</i>", "thinking")

        model_id, backend = self._get_selected_model()
        self._worker = ClaudeWorker(
            text,
            self._context,
            history=self._history[:-1],
            available_servers=self._servers,
            model_id=model_id,
            backend=backend,
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_response)
        self._worker.start()

    def _on_progress(self, status: str):
        self._progress_lines.append(status)
        visible = self._progress_lines[-10:]
        lines_html = "<br>".join(html.escape(l) for l in visible)
        if len(self._progress_lines) > 10:
            lines_html = (
                f"<i style='color:#4a5568;'>... ({len(self._progress_lines) - 10} "
                f"previous)</i><br>" + lines_html
            )
        self._update_last_bubble(
            f"<div style='font-family:monospace;font-size:11px;"
            f"line-height:1.4;'>{lines_html}</div>",
            "thinking",
        )

    def _on_response(self, text: str, is_error: bool):
        role = "error" if is_error else "assistant"
        self._update_last_bubble(_md_to_html(text), role)
        self._send_btn.setEnabled(True)
        self._input.setFocus()

        if not is_error:
            self._history.append({"role": "assistant", "text": text})
        if len(self._history) > 20:
            self._history = self._history[-20:]

        self._worker = None
