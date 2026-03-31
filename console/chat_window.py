"""Unified MCP Pipeline Console — Qt chat window.

Native chat window that routes messages through Claude Code CLI,
with access to all MCP servers in the ecosystem (maya-mcp, fpt-mcp,
flame-mcp, vision3d).

Differences from fpt-mcp console:
  - Server panel showing all MCP servers and their status
  - Multi-context support (ShotGrid entity + Maya scene)
  - Maya-blue accent theme (#00b4d8) instead of fpt-mcp red (#e94560)
  - Dynamic system prompt based on available servers
"""

from __future__ import annotations

import html
import re
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .claude_worker import ClaudeWorker
from .server_panel import ServerPanel


# ---------------------------------------------------------------------------
# Stylesheet — Maya-blue dark theme
# ---------------------------------------------------------------------------

DARK_STYLE = """
QMainWindow, QWidget#central {
    background-color: #1a1a2e;
}
QLabel#title {
    color: #00b4d8;
    font-size: 15px;
    font-weight: 700;
}
QLabel#contextBadge {
    background-color: #0f3460;
    color: #94a3b8;
    padding: 3px 10px;
    border-radius: 10px;
    font-size: 12px;
}
QLabel#contextBadge[active="true"] {
    background-color: #164e63;
    color: #67e8f9;
}
QLabel#statusDot {
    min-width: 10px;
    max-width: 10px;
    min-height: 10px;
    max-height: 10px;
    border-radius: 5px;
    background-color: #22c55e;
}
QTextBrowser#chat {
    background-color: #1a1a2e;
    color: #cbd5e1;
    border: none;
    font-size: 14px;
    selection-background-color: #334155;
}
QLineEdit#input {
    background-color: #1e293b;
    border: 1px solid #334155;
    color: #e0e0e0;
    padding: 10px 14px;
    border-radius: 10px;
    font-size: 14px;
}
QLineEdit#input:focus {
    border-color: #00b4d8;
}
QPushButton#sendBtn {
    background-color: #00b4d8;
    color: white;
    border: none;
    padding: 10px 22px;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#sendBtn:hover {
    background-color: #0096b7;
}
QPushButton#sendBtn:disabled {
    background-color: #334155;
}
QWidget#header {
    background-color: #16213e;
    border-bottom: 1px solid #0f3460;
}
QWidget#inputBar {
    background-color: #16213e;
    border-top: 1px solid #0f3460;
}
QSplitter::handle {
    background-color: #1e3a5f;
    width: 1px;
}
"""


# ---------------------------------------------------------------------------
# Minimal markdown → HTML converter (no external deps)
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
                out.append('<pre style="background:#0f172a;color:#93c5fd;'
                           'padding:10px;border-radius:6px;font-size:13px;'
                           'overflow-x:auto;">')
                in_code = True
            continue

        if in_code:
            out.append(html.escape(line))
            continue

        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            sizes = {1: "18px", 2: "16px", 3: "14px"}
            out.append(f'<p style="font-size:{sizes[level]};font-weight:700;'
                       f'color:#e0e0e0;margin:8px 0 4px;">{html.escape(m.group(2))}</p>')
            continue

        if re.match(r"^\s*[-*]\s+", line):
            content = re.sub(r"^\s*[-*]\s+", "", line)
            content = _inline_fmt(content)
            out.append(f'<p style="margin:2px 0 2px 16px;">&#8226; {content}</p>')
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
    text = re.sub(r"`([^`]+)`",
                  r'<code style="background:#1e293b;padding:2px 5px;'
                  r'border-radius:3px;color:#93c5fd;">\1</code>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  r'<a style="color:#67e8f9;" href="\2">\1</a>', text)
    return text


# ---------------------------------------------------------------------------
# Chat Window
# ---------------------------------------------------------------------------

class ChatWindow(QMainWindow):
    """Unified MCP Pipeline Console — chat + server panel."""

    def __init__(
        self,
        entity_type: str | None = None,
        entity_id: int | None = None,
        project_id: int | None = None,
        project_name: str | None = None,
        user_login: str | None = None,
    ):
        super().__init__()
        self._history: list = []
        self._context: dict = {}
        if entity_type and entity_id:
            self._context["entity_type"] = entity_type
            self._context["entity_id"] = entity_id
        if project_id:
            self._context["project_id"] = project_id
        if project_name:
            self._context["project_name"] = project_name
        if user_login:
            self._context["user_login"] = user_login

        self._worker: Optional[ClaudeWorker] = None
        self._setup_ui()
        self.setStyleSheet(DARK_STYLE)

        # Initialize server panel after UI is ready
        self._server_panel.initialize()

    def _setup_ui(self):
        self.setWindowTitle("MCP Pipeline Console")
        self.setMinimumSize(900, 550)
        self.resize(1050, 650)

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Header ──
        header = QWidget()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)

        title = QLabel("MCP Pipeline Console")
        title.setObjectName("title")
        header_layout.addWidget(title)

        # Context badges
        ctx_text = "Sin contexto"
        ctx_active = False
        if self._context.get("entity_type") and self._context.get("entity_id"):
            ctx_text = f"{self._context['entity_type']} #{self._context['entity_id']}"
            ctx_active = True

        self._context_badge = QLabel(ctx_text)
        self._context_badge.setObjectName("contextBadge")
        self._context_badge.setProperty("active", ctx_active)
        header_layout.addWidget(self._context_badge)

        header_layout.addStretch()

        status = QLabel()
        status.setObjectName("statusDot")
        header_layout.addWidget(status)

        main_layout.addWidget(header)

        # ── Body: Chat + Server Panel in splitter ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)

        # Chat area
        chat_container = QWidget()
        chat_layout = QVBoxLayout(chat_container)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        self._chat = QTextBrowser()
        self._chat.setObjectName("chat")
        self._chat.setOpenExternalLinks(True)
        self._chat.setReadOnly(True)
        self._chat.setFont(QFont("SF Pro", 13))
        chat_layout.addWidget(self._chat, 1)

        splitter.addWidget(chat_container)

        # Server panel (right side)
        self._server_panel = ServerPanel()
        splitter.addWidget(self._server_panel)

        # Set proportions: chat 75%, panel 25%
        splitter.setSizes([780, 260])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, True)

        main_layout.addWidget(splitter, 1)

        # ── Input bar ──
        input_bar = QWidget()
        input_bar.setObjectName("inputBar")
        input_layout = QHBoxLayout(input_bar)
        input_layout.setContentsMargins(16, 10, 16, 10)

        self._input = QLineEdit()
        self._input.setObjectName("input")
        self._input.setPlaceholderText("Ask anything — Maya, ShotGrid, Vision3D, Flame...")
        self._input.returnPressed.connect(self._send)
        input_layout.addWidget(self._input, 1)

        self._send_btn = QPushButton("Send")
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.clicked.connect(self._send)
        input_layout.addWidget(self._send_btn)

        main_layout.addWidget(input_bar)

        self._input.setFocus()

    # ── Context update ──

    def update_context(self, ctx: dict):
        """Update pipeline context (from protocol URL or AMI)."""
        if ctx.get("entity_type"):
            self._context["entity_type"] = ctx["entity_type"]
        if ctx.get("entity_id"):
            self._context["entity_id"] = ctx["entity_id"]
        if ctx.get("project_id"):
            self._context["project_id"] = ctx["project_id"]
        if ctx.get("project_name"):
            self._context["project_name"] = ctx["project_name"]
        if ctx.get("user_login"):
            self._context["user_login"] = ctx["user_login"]

        if self._context.get("entity_type") and self._context.get("entity_id"):
            self._context_badge.setText(
                f"{self._context['entity_type']} #{self._context['entity_id']}"
            )
            self._context_badge.setProperty("active", True)
            self._context_badge.style().unpolish(self._context_badge)
            self._context_badge.style().polish(self._context_badge)

        self.raise_()
        self.activateWindow()

    # ── Chat logic ──

    def _append_bubble(self, html_content: str, role: str):
        """Add a message bubble to the chat."""
        colors = {
            "user":      ("text-align:right;", "#0f3460", "#e0e0e0"),
            "assistant": ("text-align:left;",  "#1e293b", "#cbd5e1"),
            "error":     ("text-align:left;",  "#7f1d1d", "#fca5a5"),
            "thinking":  ("text-align:left;",  "#1e293b", "#64748b"),
        }
        align, bg, fg = colors.get(role, colors["assistant"])
        bubble = (
            f'<div style="{align}margin:6px 4px;">'
            f'<div style="display:inline-block;background:{bg};color:{fg};'
            f'padding:10px 14px;border-radius:12px;max-width:85%;'
            f'text-align:left;font-size:14px;line-height:1.6;">'
            f'{html_content}'
            f'</div></div>'
        )
        self._chat.append(bubble)

    def _send(self):
        text = self._input.text().strip()
        if not text:
            return

        self._input.clear()
        self._send_btn.setEnabled(False)
        self._append_bubble(html.escape(text), "user")

        self._history.append({"role": "user", "text": text})

        self._progress_lines = []
        self._append_bubble("<i>Thinking...</i>", "thinking")

        self._worker = ClaudeWorker(
            text,
            self._context,
            history=self._history[:-1],
            available_servers=self._server_panel.get_available_servers(),
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_response)
        self._worker.start()

    def _on_progress(self, status: str):
        """Update the thinking bubble with accumulated progress lines."""
        self._progress_lines.append(status)
        visible = self._progress_lines[-12:]
        lines_html = "<br>".join(html.escape(l) for l in visible)
        if len(self._progress_lines) > 12:
            lines_html = (
                f"<i style='color:#4a5568;'>... ({len(self._progress_lines) - 12} "
                f"previous lines)</i><br>" + lines_html
            )
        self._update_last_bubble(
            f"<div style='font-family:monospace;font-size:12px;"
            f"line-height:1.5;'>{lines_html}</div>",
            "thinking",
        )

    def _update_last_bubble(self, html_content: str, role: str):
        """Replace the last bubble with new content."""
        colors = {
            "user":      ("text-align:right;", "#0f3460", "#e0e0e0"),
            "assistant": ("text-align:left;",  "#1e293b", "#cbd5e1"),
            "error":     ("text-align:left;",  "#7f1d1d", "#fca5a5"),
            "thinking":  ("text-align:left;",  "#1e293b", "#64748b"),
        }
        align, bg, fg = colors.get(role, colors["assistant"])
        bubble = (
            f'<div style="{align}margin:6px 4px;">'
            f'<div style="display:inline-block;background:{bg};color:{fg};'
            f'padding:10px 14px;border-radius:12px;max-width:85%;'
            f'text-align:left;font-size:14px;line-height:1.6;">'
            f'{html_content}'
            f'</div></div>'
        )
        cursor = self._chat.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.movePosition(cursor.MoveOperation.StartOfBlock, cursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.deletePreviousChar()
        self._chat.setTextCursor(cursor)
        self._chat.append(bubble)

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
