"""In-vivo MCP client probe for maya-mcp visible-progress streaming.

Run with the repo venv: .venv/bin/python scripts/probe_streaming_invivo.py
Requires Maya installed on this machine (launches it if not running).

Spawns a FRESH maya-mcp stdio server from the repo venv (new code), then:
  1. maya_session(action="launch")  — expects ctx.info lines + progress
     notifications while Maya boots (or already_running if Maya is up).
  2. maya_session(action="execute_python") with a ~12s busy snippet —
     expects one heartbeat info line at the 10s mark.

Prints every logging/progress notification as it arrives, then a verdict.
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
events = {"logs": [], "progress": []}


async def on_log(params) -> None:
    line = f"[log/{params.level}] {params.data}"
    events["logs"].append(str(params.data))
    print(line, flush=True)


async def on_progress(progress: float, total: float | None, message: str | None) -> None:
    events["progress"].append((progress, total))
    print(f"[progress] {progress}/{total} {message or ''}", flush=True)


async def main() -> int:
    server = StdioServerParameters(
        command=f"{REPO}/.venv/bin/python",
        args=["-m", "maya_mcp.server"],
        cwd=REPO,
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write, logging_callback=on_log) as session:
            await session.initialize()
            print("== initialized, calling maya_session launch ==", flush=True)

            res = await session.call_tool(
                "maya_session",
                {"params": {"action": "launch"}},
                progress_callback=on_progress,
            )
            launch_out = res.content[0].text if res.content else ""
            print(f"== launch result: {launch_out[:200]}", flush=True)
            launch_data = json.loads(launch_out)

            print("== calling execute_python (12s busy) ==", flush=True)
            code = (
                "import time\n"
                "_t0 = time.time()\n"
                "while time.time() - _t0 < 12:\n"
                "    pass\n"
                "result = {'slept': round(time.time() - _t0, 1)}\n"
            )
            res2 = await session.call_tool(
                "maya_session",
                {"params": {"action": "execute_python", "params": {"code": code, "timeout": 20}}},
                progress_callback=on_progress,
            )
            exec_out = res2.content[0].text if res2.content else ""
            print(f"== exec result: {exec_out[:200]}", flush=True)

    # ── Verdict ──────────────────────────────────────────────────────────
    launched_fresh = launch_data.get("status") == "launched"
    heartbeats = [m for m in events["logs"] if "still running in Maya" in m]
    launch_infos = [m for m in events["logs"] if "Command Port" in m or "Maya ready" in m]

    print("\n== VERDICT ==")
    print(f"launch status:        {launch_data.get('status', launch_data)}")
    print(f"launch info lines:    {len(launch_infos)}")
    print(f"progress events:      {len(events['progress'])}")
    print(f"exec heartbeats:      {len(heartbeats)}")
    ok_launch = (not launched_fresh) or (launch_infos and events["progress"])
    ok_exec = len(heartbeats) >= 1 and "slept" in exec_out
    print(f"OK launch streaming:  {ok_launch} (fresh launch: {launched_fresh})")
    print(f"OK exec heartbeat:    {ok_exec}")
    return 0 if (ok_launch and ok_exec) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
