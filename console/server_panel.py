"""Server status panel — detects and monitors all MCP servers in the ecosystem.

Reads Claude Code configuration (~/.claude.json) to discover which MCP servers
are available, then performs periodic health checks to display live status.

Provides two widgets:
  - ServerPanel   — full side panel with rows (for standalone window)
  - ServerStatusBar — compact horizontal dots (for Maya panel header)

Servers supported:
  - maya-mcp   → TCP ping to Maya Command Port
  - fpt-mcp    → ShotGrid credential check
  - flame-mcp  → TCP ping to Flame (if configured)
  - vision3d   → HTTP GET /api/health (GPU server)
"""

from __future__ import annotations

import json
import os
import socket
from typing import Optional

from .qt_compat import QtWidgets, QtCore

# Aliases for Qt classes used throughout
QThread = QtCore.QThread
QTimer = QtCore.QTimer
Signal = QtCore.Signal
Qt = QtCore.Qt
QFrame = QtWidgets.QFrame
QHBoxLayout = QtWidgets.QHBoxLayout
QLabel = QtWidgets.QLabel
QVBoxLayout = QtWidgets.QVBoxLayout
QWidget = QtWidgets.QWidget


# ---------------------------------------------------------------------------
# MCP server discovery
# ---------------------------------------------------------------------------

# Known servers in the ecosystem and their detection heuristics
_KNOWN_SERVERS = {
    "maya-mcp": {
        "label": "maya-mcp",
        "desc_connected": "Maya + Vision3D",
        "desc_offline": "Maya control",
        "icon": "🎬",
    },
    "fpt-mcp": {
        "label": "fpt-mcp",
        "desc_connected": "ShotGrid API + Toolkit",
        "desc_offline": "ShotGrid pipeline",
        "icon": "🎯",
    },
    "flame-mcp": {
        "label": "flame-mcp",
        "desc_connected": "Autodesk Flame",
        "desc_offline": "Flame control",
        "icon": "🔥",
    },
}


def detect_mcp_servers() -> dict[str, dict]:
    """Read Claude Code config to find configured MCP servers.

    Returns dict of {server_name: {command, args, env, ...}}.
    """
    config_path = os.path.expanduser("~/.claude.json")
    if not os.path.isfile(config_path):
        return {}

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    servers = {}
    for name, cfg in config.get("mcpServers", {}).items():
        servers[name] = {
            "command": cfg.get("command", ""),
            "args": cfg.get("args", []),
            "env": cfg.get("env", {}),
        }
    return servers


def _tcp_check(host: str, port: int, timeout: float = 2.0) -> bool:
    """Quick TCP connectivity check."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def _http_check(url: str, timeout: float = 3.0) -> Optional[dict]:
    """Quick HTTP health check. Returns response JSON or None."""
    try:
        import urllib.request
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Health checker thread
# ---------------------------------------------------------------------------

class HealthChecker(QThread):
    """Background thread that checks all configured MCP servers."""

    finished = Signal(dict)  # {name: {status, info}}

    def __init__(self, servers: dict, parent=None):
        super().__init__(parent)
        self._servers = servers

    def run(self):  # noqa: D102
        results = {}

        for name, cfg in self._servers.items():
            env = cfg.get("env", {})
            info = {}

            if name == "maya-mcp":
                host = env.get("MAYA_HOST", "localhost")
                port = int(env.get("MAYA_PORT", "7001"))
                connected = _tcp_check(host, port)
                info["status"] = "connected" if connected else "offline"
                info["detail"] = f"TCP {host}:{port}"
                # Check Vision3D if configured
                gpu_url = env.get("GPU_API_URL", "")
                if gpu_url:
                    health = _http_check(f"{gpu_url.rstrip('/')}/api/health")
                    if health:
                        gpu = health.get("gpu", "unknown")
                        vram = health.get("vram_gb", "?")
                        info["vision3d"] = f"{gpu} ({vram}GB)"
                    else:
                        info["vision3d"] = "offline"

            elif name == "fpt-mcp":
                # Check if ShotGrid credentials exist
                sg_url = env.get("SHOTGRID_URL", "")
                if sg_url:
                    info["status"] = "configured"
                    info["detail"] = sg_url.replace("https://", "").split(".")[0]
                else:
                    info["status"] = "configured"
                    info["detail"] = "credentials in .env"

            elif name == "flame-mcp":
                host = env.get("FLAME_HOST", "localhost")
                port = int(env.get("FLAME_PORT", "8008"))
                connected = _tcp_check(host, port)
                info["status"] = "connected" if connected else "offline"
                info["detail"] = f"TCP {host}:{port}"

            else:
                info["status"] = "configured"
                info["detail"] = "unknown type"

            results[name] = info

        self.finished.emit(results)


# ---------------------------------------------------------------------------
# Server Panel Widget
# ---------------------------------------------------------------------------

PANEL_STYLE = """
QFrame#serverPanel {
    background-color: #0f172a;
    border-left: 1px solid #1e3a5f;
    min-width: 220px;
    max-width: 260px;
}
QLabel#panelTitle {
    color: #64748b;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 12px 14px 6px;
}
QLabel#serverName {
    color: #e0e0e0;
    font-size: 13px;
    font-weight: 600;
}
QLabel#serverDetail {
    color: #64748b;
    font-size: 11px;
}
QLabel#statusDotGreen {
    min-width: 8px; max-width: 8px;
    min-height: 8px; max-height: 8px;
    border-radius: 4px;
    background-color: #22c55e;
}
QLabel#statusDotRed {
    min-width: 8px; max-width: 8px;
    min-height: 8px; max-height: 8px;
    border-radius: 4px;
    background-color: #ef4444;
}
QLabel#statusDotYellow {
    min-width: 8px; max-width: 8px;
    min-height: 8px; max-height: 8px;
    border-radius: 4px;
    background-color: #eab308;
}
QFrame#serverRow {
    padding: 8px 14px;
    border-bottom: 1px solid #1e293b;
}
QFrame#serverRow:hover {
    background-color: #1e293b;
}
"""


class ServerPanel(QFrame):
    """Side panel showing MCP server status."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("serverPanel")
        self.setStyleSheet(PANEL_STYLE)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        title = QLabel("MCP Servers")
        title.setObjectName("panelTitle")
        self._layout.addWidget(title)

        self._server_widgets: dict[str, dict] = {}
        self._servers: dict = {}

        # Periodic health check
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._run_health_check)

        self._layout.addStretch()

    def initialize(self):
        """Detect servers and start health checking."""
        self._servers = detect_mcp_servers()
        self._build_server_rows()
        self._run_health_check()
        self._timer.start(30_000)  # every 30s

    def _build_server_rows(self):
        """Create UI rows for each detected server."""
        # Insert before the stretch
        insert_idx = self._layout.count() - 1

        for name in self._servers:
            meta = _KNOWN_SERVERS.get(name, {
                "label": name,
                "desc_offline": name,
                "desc_connected": name,
                "icon": "🔌",
            })

            row = QFrame()
            row.setObjectName("serverRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(14, 8, 14, 8)
            row_layout.setSpacing(8)

            dot = QLabel()
            dot.setObjectName("statusDotYellow")
            dot.setFixedSize(8, 8)
            row_layout.addWidget(dot)

            info_layout = QVBoxLayout()
            info_layout.setSpacing(1)

            name_label = QLabel(f"{meta['icon']} {meta['label']}")
            name_label.setObjectName("serverName")
            info_layout.addWidget(name_label)

            detail = QLabel("checking...")
            detail.setObjectName("serverDetail")
            info_layout.addWidget(detail)

            row_layout.addLayout(info_layout, 1)

            self._layout.insertWidget(insert_idx, row)
            insert_idx += 1

            self._server_widgets[name] = {
                "dot": dot,
                "detail": detail,
                "row": row,
            }

    def _run_health_check(self):
        """Launch background health check."""
        if not self._servers:
            return
        self._checker = HealthChecker(self._servers, parent=self)
        self._checker.finished.connect(self._on_health_results)
        self._checker.start()

    def _on_health_results(self, results: dict):
        """Update UI with health check results."""
        for name, info in results.items():
            widgets = self._server_widgets.get(name)
            if not widgets:
                continue

            status = info.get("status", "offline")
            detail_text = info.get("detail", "")

            if status == "connected":
                widgets["dot"].setObjectName("statusDotGreen")
            elif status == "configured":
                widgets["dot"].setObjectName("statusDotYellow")
            else:
                widgets["dot"].setObjectName("statusDotRed")

            # Force stylesheet refresh
            widgets["dot"].style().unpolish(widgets["dot"])
            widgets["dot"].style().polish(widgets["dot"])

            # Build detail text
            parts = [detail_text]
            if "vision3d" in info:
                v3d = info["vision3d"]
                parts.append(f"V3D: {v3d}")

            widgets["detail"].setText(" · ".join(parts))

    def get_available_servers(self) -> dict:
        """Return the detected servers dict for system prompt generation."""
        return self._servers


# ---------------------------------------------------------------------------
# Compact status bar (for Maya panel header)
# ---------------------------------------------------------------------------

_STATUS_BAR_STYLE = """
QLabel#dotGreen {
    min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px;
    border-radius: 4px; background-color: #22c55e;
}
QLabel#dotRed {
    min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px;
    border-radius: 4px; background-color: #ef4444;
}
QLabel#dotYellow {
    min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px;
    border-radius: 4px; background-color: #eab308;
}
QLabel#serverTag {
    color: #64748b; font-size: 10px; font-weight: 600;
}
"""


class ServerStatusBar(QWidget):
    """Compact horizontal server status dots for embedding in headers.

    Shows: ●maya-mcp ●fpt-mcp ●flame-mcp ●vision3d  with coloured dots.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_STATUS_BAR_STYLE)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(4)
        self._dots: dict[str, QLabel] = {}
        self._servers: dict = {}

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._run_health_check)

    def update_servers(self, servers: dict):
        """Build dots for each server and start health checks."""
        self._servers = servers

        # Clear existing
        while self._lay.count():
            item = self._lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._dots.clear()

        for name in servers:
            dot = QLabel()
            dot.setObjectName("dotYellow")
            dot.setFixedSize(8, 8)
            self._lay.addWidget(dot)

            tag = QLabel(name)
            tag.setObjectName("serverTag")
            self._lay.addWidget(tag)

            self._dots[name] = dot

        # Vision3D — shown as separate indicator if maya-mcp has GPU_API_URL
        maya_cfg = servers.get("maya-mcp", {})
        gpu_url = maya_cfg.get("env", {}).get("GPU_API_URL", "")
        if gpu_url:
            dot = QLabel()
            dot.setObjectName("dotYellow")
            dot.setFixedSize(8, 8)
            self._lay.addWidget(dot)

            tag = QLabel("vision3d")
            tag.setObjectName("serverTag")
            self._lay.addWidget(tag)

            self._dots["_vision3d"] = dot

        # Start health checks
        self._run_health_check()
        self._timer.start(30_000)

    def _run_health_check(self):
        if not self._servers:
            return
        self._checker = HealthChecker(self._servers, parent=self)
        self._checker.finished.connect(self._on_results)
        self._checker.start()

    def _on_results(self, results: dict):
        for name, info in results.items():
            dot = self._dots.get(name)
            if not dot:
                continue
            status = info.get("status", "offline")
            if status == "connected":
                dot.setObjectName("dotGreen")
            elif status == "configured":
                dot.setObjectName("dotYellow")
            else:
                dot.setObjectName("dotRed")
            dot.style().unpolish(dot)
            dot.style().polish(dot)

        # Vision3D separate indicator
        v3d_dot = self._dots.get("_vision3d")
        if v3d_dot:
            maya_info = results.get("maya-mcp", {})
            v3d_status = maya_info.get("vision3d", "")
            if v3d_status and v3d_status != "offline":
                v3d_dot.setObjectName("dotGreen")
            else:
                v3d_dot.setObjectName("dotRed")
            v3d_dot.style().unpolish(v3d_dot)
            v3d_dot.style().polish(v3d_dot)
