"""MCP Pipeline Console — entry point and protocol handler.

Unified console for the entire MCP ecosystem (maya-mcp, fpt-mcp, flame-mcp,
vision3d). On macOS, protocol URLs arrive via Apple Events (kAEGetURL),
which Qt translates to QFileOpenEvent.

Supports:
  - maya-mcp:// protocol URLs (from Maya shelf, ShotGrid AMI, etc.)
  - CLI arguments (--entity-type, --entity-id, --project-id, etc.)
  - No arguments (standalone console with auto-detected MCP servers)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QEvent, QTimer, QThread, Signal
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication

from .chat_window import ChatWindow


# ---------------------------------------------------------------------------
# ShotGrid Light Payload resolver (optional — works if shotgun_api3 installed)
# ---------------------------------------------------------------------------

def _load_sg_credentials() -> dict:
    """Load ShotGrid credentials from .env files (check fpt-mcp and local)."""
    env_paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        os.path.expanduser("~/Claude_projects/fpt-mcp/.env"),
    ]
    creds = {}
    for env_path in env_paths:
        if os.path.isfile(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        key = k.strip()
                        if key not in creds:
                            creds[key] = v.strip().strip('"').strip("'")
    return creds


def fetch_ami_payload(event_log_entry_id: int) -> dict:
    """Fetch AMI context from ShotGrid EventLogEntry (Light Payload)."""
    try:
        import shotgun_api3
        creds = _load_sg_credentials()
        required = ("SHOTGRID_URL", "SHOTGRID_SCRIPT_NAME", "SHOTGRID_SCRIPT_KEY")
        if not all(k in creds for k in required):
            print("[ami_payload] Missing SG credentials", flush=True)
            return {}

        sg = shotgun_api3.Shotgun(
            creds["SHOTGRID_URL"],
            script_name=creds["SHOTGRID_SCRIPT_NAME"],
            api_key=creds["SHOTGRID_SCRIPT_KEY"],
        )

        entry = sg.find_one(
            "EventLogEntry",
            [["id", "is", event_log_entry_id]],
            ["meta"],
        )
        if not entry or not entry.get("meta"):
            return {}

        meta = entry["meta"]
        payload = meta.get("ami_payload", meta)

        result = {}
        if "entity_type" in payload:
            result["entity_type"] = payload["entity_type"]
        ids = payload.get("ids") or payload.get("selected_ids")
        if ids:
            if isinstance(ids, list):
                result["entity_id"] = int(ids[0])
            elif isinstance(ids, str):
                result["entity_id"] = int(ids.split(",")[0])
            elif isinstance(ids, int):
                result["entity_id"] = ids
        if "project_id" in payload:
            result["project_id"] = int(payload["project_id"])
        if "project_name" in payload:
            result["project_name"] = payload["project_name"]
        if "user_login" in payload:
            result["user_login"] = payload["user_login"]

        return result

    except ImportError:
        print("[ami_payload] shotgun_api3 not installed — Light Payload disabled", flush=True)
        return {}
    except Exception as e:
        print(f"[ami_payload] ERROR: {e}", flush=True)
        return {}


class PayloadFetcher(QThread):
    """Fetches AMI payload from ShotGrid API in background."""

    finished = Signal(dict)

    def __init__(self, event_log_entry_id: int, parent=None):
        super().__init__(parent)
        self._id = event_log_entry_id

    def run(self):
        ctx = fetch_ami_payload(self._id)
        self.finished.emit(ctx)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

PROTOCOL_SCHEME = "maya-mcp"


def parse_protocol_url(url: str) -> tuple[dict, int | None]:
    """Parse a maya-mcp:// URL into context params.

    Returns (context_dict, event_log_entry_id_or_None).
    """
    event_log_id = None
    m = re.search(r"event_log_entry_id=(\d+)", url)
    if m:
        event_log_id = int(m.group(1))

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    result = {}

    if "entity_type" in qs:
        val = qs["entity_type"][0]
        if not val.startswith("{"):
            result["entity_type"] = val

    for key in ("ids", "selected_ids"):
        if key in qs:
            try:
                val = qs[key][0].split(",")[0]
                if not val.startswith("{"):
                    result["entity_id"] = int(val)
                    break
            except (ValueError, IndexError):
                pass

    if "project_id" in qs:
        try:
            val = qs["project_id"][0]
            if not val.startswith("{"):
                result["project_id"] = int(val)
        except (ValueError, IndexError):
            pass

    if "project_name" in qs:
        val = qs["project_name"][0]
        if not val.startswith("{"):
            result["project_name"] = val

    if "user_login" in qs:
        val = qs["user_login"][0]
        if not val.startswith("{"):
            result["user_login"] = val

    return result, event_log_id


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class PipelineApplication(QApplication):
    """Custom QApplication that handles macOS protocol URL Apple Events."""

    def __init__(self, argv):
        super().__init__(argv)
        self.window: ChatWindow | None = None
        self._pending_url: str | None = None

    def event(self, event: QEvent) -> bool:
        try:
            if isinstance(event, QFileOpenEvent):
                url = event.url().toString()
                print(f"[QFileOpenEvent] url={url}", flush=True)
                if url and url.startswith(f"{PROTOCOL_SCHEME}://"):
                    if self.window:
                        self._process_url(url)
                    else:
                        self._pending_url = url
                    return True
        except Exception as e:
            print(f"[QFileOpenEvent] ERROR: {e}", flush=True)
        return super().event(event)

    def _process_url(self, url: str):
        """Parse URL and fetch context from API if needed."""
        ctx, event_log_id = parse_protocol_url(url)
        print(f"[process] ctx={ctx} event_log_id={event_log_id}", flush=True)

        if ctx.get("entity_type") and ctx.get("entity_id"):
            if self.window:
                self.window.update_context(ctx)
        elif event_log_id:
            self._fetcher = PayloadFetcher(event_log_id, parent=self)
            self._fetcher.finished.connect(self._on_payload_fetched)
            self._fetcher.start()

    def _on_payload_fetched(self, ctx: dict):
        print(f"[payload_fetched] ctx={ctx}", flush=True)
        if ctx and self.window:
            self.window.update_context(ctx)

    def process_pending(self):
        """Process URL that arrived before window was created."""
        if self._pending_url:
            url = self._pending_url
            self._pending_url = None
            self._process_url(url)


def main():
    print(f"[main] sys.argv={sys.argv}", flush=True)

    parser = argparse.ArgumentParser(description="MCP Pipeline Console")
    parser.add_argument("--entity-type", type=str, default=None)
    parser.add_argument("--entity-id", type=int, default=None)
    parser.add_argument("--project-id", type=int, default=None)
    parser.add_argument("--project-name", type=str, default=None)
    parser.add_argument("--user-login", type=str, default=None)
    parser.add_argument("url", nargs="?", default=None,
                        help="maya-mcp:// protocol URL")

    args, unknown = parser.parse_known_args()

    # Build context from CLI arguments
    ctx = {}
    all_args = ([args.url] if args.url else []) + unknown
    for arg in all_args:
        if arg and arg.startswith(f"{PROTOCOL_SCHEME}://"):
            parsed_ctx, _ = parse_protocol_url(arg)
            if parsed_ctx:
                ctx = parsed_ctx
            break

    if not ctx:
        if args.entity_type:
            ctx["entity_type"] = args.entity_type
        if args.entity_id:
            ctx["entity_id"] = args.entity_id
        if args.project_id:
            ctx["project_id"] = args.project_id
        if args.project_name:
            ctx["project_name"] = args.project_name
        if args.user_login:
            ctx["user_login"] = args.user_login

    app = PipelineApplication(sys.argv)
    app.setApplicationName("MCP Pipeline Console")
    app.setOrganizationName("maya-mcp")

    print(f"[main] initial ctx={ctx}", flush=True)

    window = ChatWindow(
        entity_type=ctx.get("entity_type"),
        entity_id=ctx.get("entity_id"),
        project_id=ctx.get("project_id"),
        project_name=ctx.get("project_name"),
        user_login=ctx.get("user_login"),
    )
    app.window = window
    window.show()

    QTimer.singleShot(100, app.process_pending)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
